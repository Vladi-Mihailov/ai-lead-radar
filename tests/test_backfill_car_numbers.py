"""Тесты узкого backfill car_numbers (reader/users/backfill_car_numbers.py)
— читает историю сообщений (тот же приём, что и history_sync.py), но
трогает СТРОГО ТОЛЬКО users.car_numbers: не пересчитывает keywords, не
резолвит/меняет username/access_hash/last_seen_at и не читает/пишет
HistorySyncStateRepository (checkpoint sync_users.py остаётся нетронутым).

Отдельный блок тестов (см. "---- crash/resume ----" ниже) проверяет
периодический flush и его СВОЙ checkpoint (CarNumbersBackfillStateRepository,
car_numbers_backfill_state) — историю произвольного размера нужно уметь
обрабатывать пакетами, переживая обрыв процесса без потери уже найденных
номеров и без повторного чтения истории с самого начала.

Реальные запросы к Telegram не выполняются — TelegramClient полностью
заменён фейком, как и в tests/test_history_sync.py.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from telethon.errors import FloodWaitError  # noqa: E402
from telethon.tl.functions.messages import GetHistoryRequest  # noqa: E402

import reader.users.backfill_car_numbers as backfill_module  # noqa: E402
from reader.groups import Group  # noqa: E402
from reader.users.backfill_car_numbers import backfill_car_numbers  # noqa: E402
from reader.users.car_numbers_backfill_state import (  # noqa: E402
    CarNumbersBackfillStateRepository,
)
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


class _SimulatedCrash(BaseException):
    """Имитирует настоящий обрыв процесса (SIGKILL/оборванный SSH/
    перезагрузка сервера) — намеренно наследуется от BaseException, а не
    Exception, поэтому НЕ перехватывается ни одним except-блоком внутри
    backfill_car_numbers._scan_group (как и реальный обрыв процесса, у
    которого нет ни единого шанса выполнить finally/flush)."""


class _CrashingFakeClient:
    """Как _FakeClient, но после того, как consumer ПОЛНОСТЬЮ обработает
    crash_after-е сообщение (включая любой flush, сработавший именно на
    этом сообщении), вместо следующего сообщения бросает _SimulatedCrash —
    то есть обрывается ровно так, как выглядел бы настоящий обрыв процесса
    между обработкой сообщений N и N+1."""

    def __init__(self, entity, messages, *, crash_after):
        self.entity = entity
        self.messages = messages
        self.crash_after = crash_after
        self.iter_messages_offsets: list[int] = []

    async def get_entity(self, ident):
        return self.entity

    def iter_messages(self, entity, offset_id=0, limit=None):
        self.iter_messages_offsets.append(offset_id)
        crash_after = self.crash_after
        available = [m for m in self.messages if offset_id == 0 or m.id < offset_id]

        async def gen():
            for count, message in enumerate(available, start=1):
                yield message
                if count >= crash_after:
                    raise _SimulatedCrash()

        return gen()


def _make_messages(entries):
    """entries — [(message_id, sender_id, raw_text), ...] по убыванию id."""
    return [_FakeMessage(mid, sender_id, text) for mid, sender_id, text in entries]


def _make_big_history(total: int, *, users: int = 5) -> list[_FakeMessage]:
    """total сообщений с id по убыванию от total до 1 (как настоящая
    история Telegram), от `users` разных отправителей по кругу — у каждого
    свой фиксированный номер, чтобы результат не зависел от того, сколько
    раз сообщения того или иного пользователя реально обработались (важно
    для тестов crash/resume — идемпотентность должна давать РОВНО один
    номер на пользователя, сколько бы раз историю ни читали повторно)."""
    messages = []
    for index in range(total):
        message_id = total - index
        sender_id = (index % users) + 1
        plate = f"A{sender_id:03d}AA77"
        messages.append(_FakeMessage(message_id, sender_id, f"еду {plate}"))
    return messages


async def _sleep_recorder(monkeypatch):
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr("reader.users.backfill_car_numbers.asyncio.sleep", fake_sleep)
    return calls


def _open_repositories(db_path):
    return UserRepository(db_path), CarNumbersBackfillStateRepository(db_path)


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

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    try:
        stats = await backfill_car_numbers(client, [group], repository, state_repository)

        assert repository.get_car_numbers(111) == ["A111AA77"]
        assert repository.get_car_numbers(222) == ["X777XX197"]
        assert stats.groups_scanned == 1
        assert stats.messages_scanned == 3
        assert stats.users_with_car_numbers == 2

        checkpoint = state_repository.get(-100111)
        assert checkpoint.completed is True
        assert checkpoint.last_message_id == 1
    finally:
        repository.close()
        state_repository.close()


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

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    try:
        await backfill_car_numbers(_MultiGroupClient(), [group_a, group_b], repository, state_repository)

        assert repository.get_car_numbers(333) == ["A111AA77", "X777XX197"]
    finally:
        repository.close()
        state_repository.close()


async def test_message_without_sender_id_is_skipped(tmp_path):
    entity = _FakeEntity(-100301, "Test group")
    group = Group(id=-100301, username=None, title="Test group")
    messages = _make_messages([(1, None, "А111АА77")])
    client = _FakeClient(entity, messages)

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    try:
        stats = await backfill_car_numbers(client, [group], repository, state_repository)

        assert stats.users_with_car_numbers == 0
        assert stats.messages_scanned == 1
    finally:
        repository.close()
        state_repository.close()


async def test_message_without_car_number_contributes_nothing(tmp_path):
    entity = _FakeEntity(-100302, "Test group")
    group = Group(id=-100302, username=None, title="Test group")
    messages = _make_messages([(1, 444, "привет, как дела?")])
    client = _FakeClient(entity, messages)

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    try:
        stats = await backfill_car_numbers(client, [group], repository, state_repository)

        assert repository.get_car_numbers(444) == []
        assert stats.users_with_car_numbers == 0
    finally:
        repository.close()
        state_repository.close()


async def test_unresolvable_group_is_skipped_without_raising(tmp_path):
    group = Group(id=-100303, username=None, title="Broken group")
    client = _FakeClient(
        _FakeEntity(-100303, "Broken group"), [], entity_error=ValueError("not found"),
    )

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    try:
        stats = await backfill_car_numbers(client, [group], repository, state_repository)

        assert stats.groups_scanned == 1
        assert stats.messages_scanned == 0
        # Группа даже не резолвилась — записывать checkpoint не для чего.
        assert state_repository.get(-100303) is None
    finally:
        repository.close()
        state_repository.close()


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

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    try:
        stats = await backfill_car_numbers(client, [group], repository, state_repository)

        assert sleep_calls == [9]
        assert client.iter_messages_calls == 2
        # Первая попытка ничего не обработала (FloodWait сразу) — offset_id
        # для повтора остался 0, вторая попытка читает всю историю заново.
        assert client.iter_messages_offsets == [0, 0]
        assert repository.get_car_numbers(555) == ["A111AA77", "X777XX197", "Y111YY77"]
        assert stats.messages_scanned == 3
    finally:
        repository.close()
        state_repository.close()


# ---- идемпотентность повторного запуска ----


async def test_rerunning_backfill_does_not_duplicate_or_change_result(tmp_path):
    entity = _FakeEntity(-100501, "Test group")
    group = Group(id=-100501, username=None, title="Test group")
    messages = _make_messages([
        (2, 666, "А111АА77"),
        (1, 666, "напомню A111AA77, и Х777ХХ197"),
    ])

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    try:
        first_client = _FakeClient(entity, messages)
        await backfill_car_numbers(first_client, [group], repository, state_repository)
        first_result = repository.get_car_numbers(666)
        assert first_result == ["A111AA77", "X777XX197"]

        # Группа уже completed — второй прогон её даже не перечитывает.
        second_client = _FakeClient(entity, messages)
        await backfill_car_numbers(second_client, [group], repository, state_repository)
        assert repository.get_car_numbers(666) == first_result
        assert second_client.iter_messages_calls == 0
    finally:
        repository.close()
        state_repository.close()


async def test_existing_car_numbers_are_merged_with_newly_found(tmp_path):
    """Номер, уже сохранённый раньше (например, live-сбором car_numbers в
    Pipeline), должен объединиться с найденным backfill'ом, а не
    перезаписаться."""
    entity = _FakeEntity(-100502, "Test group")
    group = Group(id=-100502, username=None, title="Test group")
    messages = _make_messages([(1, 700, "мой номер Х777ХХ197")])
    client = _FakeClient(entity, messages)

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    try:
        repository.add_car_numbers(700, ["A111AA77"])

        await backfill_car_numbers(client, [group], repository, state_repository)

        assert repository.get_car_numbers(700) == ["A111AA77", "X777XX197"]
    finally:
        repository.close()
        state_repository.close()


# ---- узость: НЕ трогает keywords/username/access_hash/last_seen_at/checkpoint ----


async def test_backfill_only_changes_car_numbers_leaves_everything_else_untouched(tmp_path):
    """Явное доказательство узости backfill: после прогона car_numbers
    заполнен, а keywords и все остальные поля пользователя (username,
    first_name, last_name, access_hash, peer_type, last_seen_at) остаются
    БИТ В БИТ такими же, как были до backfill."""
    db_path = tmp_path / "users.db"
    repository, state_repository = _open_repositories(db_path)
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

        await backfill_car_numbers(client, [group], repository, state_repository)

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
        state_repository.close()


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
    backfill_state_repository = CarNumbersBackfillStateRepository(db_path)
    try:
        entity = _FakeEntity(-100701, "Test group")
        group = Group(id=-100701, username=None, title="Test group")
        messages = _make_messages([(1, 888, "А111АА77")])
        client = _FakeClient(entity, messages)

        await backfill_car_numbers(client, [group], repository, backfill_state_repository)
        assert repository.get_car_numbers(888) == ["A111AA77"]
    finally:
        repository.close()
        backfill_state_repository.close()

    state_repository = HistorySyncStateRepository(db_path)
    try:
        assert state_repository.get(-100701) == before
        assert state_repository.get(-100701, mode="reindex") == before_reindex
    finally:
        state_repository.close()


# ---- crash/resume: периодический flush и свой checkpoint ----


async def test_flush_happens_periodically_not_only_at_group_end(tmp_path, monkeypatch):
    """Номера должны попадать в БД уже после первого пакета, а не только в
    самом конце всей истории группы — и checkpoint должен продвигаться на
    каждом таком flush'е, включая последний неполный пакет."""
    monkeypatch.setattr(backfill_module, "FLUSH_EVERY_MESSAGES", 10)

    entity = _FakeEntity(-101001, "Periodic flush group")
    group = Group(id=-101001, username=None, title="Periodic flush group")
    messages = _make_big_history(25)  # 2 полных пакета по 10 + "хвост" из 5
    client = _FakeClient(entity, messages)

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    checkpoints_seen = []
    original_save = state_repository.save_progress

    def spy_save(**kwargs):
        original_save(**kwargs)
        checkpoints_seen.append(kwargs["last_message_id"])

    monkeypatch.setattr(state_repository, "save_progress", spy_save)

    try:
        await backfill_car_numbers(client, [group], repository, state_repository)

        # 3 checkpoint'а: после 10-го, после 20-го сообщения и финальный
        # (после оставшихся 5, "хвост") — не только один раз в конце.
        assert checkpoints_seen == [16, 6, 1]

        final_checkpoint = state_repository.get(-101001)
        assert final_checkpoint.completed is True
        assert final_checkpoint.last_message_id == 1
    finally:
        repository.close()
        state_repository.close()


async def test_accumulator_is_cleared_after_each_flush(tmp_path, monkeypatch):
    """Если бы pending_numbers не очищался после flush, более поздний
    flush заново включал бы уже записанных ранее пользователей — проверяем
    это, делая flush после КАЖДОГО сообщения (FLUSH_EVERY_MESSAGES=1) с
    разными отправителями."""
    monkeypatch.setattr(backfill_module, "FLUSH_EVERY_MESSAGES", 1)

    entity = _FakeEntity(-101101, "Clear group")
    group = Group(id=-101101, username=None, title="Clear group")
    messages = _make_messages([
        (3, 1, "А001АА77"),
        (2, 2, "А002АА77"),
        (1, 3, "А003АА77"),
    ])
    client = _FakeClient(entity, messages)

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    calls = []
    original_add = repository.add_car_numbers

    def spy_add(user_id, car_numbers):
        calls.append((user_id, tuple(car_numbers)))
        original_add(user_id, car_numbers)

    monkeypatch.setattr(repository, "add_car_numbers", spy_add)

    try:
        await backfill_car_numbers(client, [group], repository, state_repository)

        assert calls == [
            (1, ("A001AA77",)),
            (2, ("A002AA77",)),
            (3, ("A003AA77",)),
        ]
    finally:
        repository.close()
        state_repository.close()


async def test_car_numbers_saved_before_checkpoint_on_each_flush(tmp_path, monkeypatch):
    """Порядок внутри КАЖДОГО flush'а: сначала все add_car_numbers этого
    пакета, и только потом ровно один save_progress — никогда наоборот."""
    monkeypatch.setattr(backfill_module, "FLUSH_EVERY_MESSAGES", 2)

    entity = _FakeEntity(-101601, "Order group")
    group = Group(id=-101601, username=None, title="Order group")
    messages = _make_messages([
        (4, 1, "А111АА77"), (3, 2, "А222АА77"), (2, 3, "А333АА77"), (1, 4, "А444АА77"),
    ])
    client = _FakeClient(entity, messages)

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    call_order = []
    original_add = repository.add_car_numbers
    original_save = state_repository.save_progress

    def spy_add(user_id, car_numbers):
        call_order.append("add_car_numbers")
        original_add(user_id, car_numbers)

    def spy_save(**kwargs):
        call_order.append("save_progress")
        original_save(**kwargs)

    monkeypatch.setattr(repository, "add_car_numbers", spy_add)
    monkeypatch.setattr(state_repository, "save_progress", spy_save)

    try:
        await backfill_car_numbers(client, [group], repository, state_repository)

        # 2 периодических flush'а (по 2 сообщения) + финальный flush,
        # помечающий группу completed=True — он не находит новых номеров
        # (предыдущий периодический flush уже всё зафлушил и очистил
        # накопитель), поэтому состоит из одного save_progress без единого
        # add_car_numbers перед ним.
        assert call_order == [
            "add_car_numbers", "add_car_numbers", "save_progress",
            "add_car_numbers", "add_car_numbers", "save_progress",
            "save_progress",
        ]
    finally:
        repository.close()
        state_repository.close()


async def test_checkpoint_not_advanced_if_car_numbers_save_fails(tmp_path, monkeypatch):
    """Если add_car_numbers падает во время flush'а — checkpoint не должен
    продвинуться дальше ПРЕДЫДУЩЕГО успешного flush'а, а уже записанные до
    сбоя номера не теряются."""
    monkeypatch.setattr(backfill_module, "FLUSH_EVERY_MESSAGES", 5)

    entity = _FakeEntity(-101701, "Failing group")
    group = Group(id=-101701, username=None, title="Failing group")
    # 10 сообщений: первые 5 (пользователи 1..5) должны успешно
    # зафлушиться, шестой вызов add_car_numbers (первый во втором пакете)
    # искусственно падает.
    messages = _make_big_history(10)
    client = _FakeClient(entity, messages)

    repository, state_repository = _open_repositories(tmp_path / "users.db")
    call_count = {"n": 0}
    original_add = repository.add_car_numbers

    def failing_add(user_id, car_numbers):
        call_count["n"] += 1
        if call_count["n"] == 6:
            raise RuntimeError("simulated write failure")
        original_add(user_id, car_numbers)

    monkeypatch.setattr(repository, "add_car_numbers", failing_add)

    try:
        await backfill_car_numbers(client, [group], repository, state_repository)

        checkpoint = state_repository.get(-101701)
        assert checkpoint is not None
        assert checkpoint.completed is False
        # Checkpoint остался на позиции ПЕРВОГО (успешного) flush.
        assert checkpoint.last_message_id == 10 - 5 + 1  # id 6

        for user_id in range(1, 6):
            assert repository.get_car_numbers(user_id) == [f"A{user_id:03d}AA77"]
    finally:
        repository.close()
        state_repository.close()


async def test_crash_between_car_numbers_saved_and_checkpoint_saved_is_safe_to_reprocess(tmp_path):
    """Ровно тот инцидент из задачи: car_numbers уже записаны и
    закоммичены, а checkpoint ещё нет — процесс "падает" именно в этот
    момент. Повторная обработка тех же сообщений при следующем запуске не
    создаёт дублей (add_car_numbers идемпотентен)."""
    db_path = tmp_path / "users.db"
    repository, state_repository = _open_repositories(db_path)
    try:
        # Симулируем "успешный save car_numbers, но не успевший checkpoint".
        repository.add_car_numbers(999, ["A111AA77"])
        assert state_repository.get(-101201) is None

        entity = _FakeEntity(-101201, "Gap group")
        group = Group(id=-101201, username=None, title="Gap group")
        messages = _make_messages([(1, 999, "А111АА77")])
        client = _FakeClient(entity, messages)

        await backfill_car_numbers(client, [group], repository, state_repository)

        assert repository.get_car_numbers(999) == ["A111AA77"]  # без дублей
        assert state_repository.get(-101201).completed is True
    finally:
        repository.close()
        state_repository.close()


async def test_crash_after_partial_batch_resumes_without_refetching_completed_batches(tmp_path):
    """История в 100 000 сообщений, batch=10 000, процесс "падает" сразу
    после 57 000-го сообщения (BaseException — как настоящий обрыв, без
    единого шанса на graceful cleanup). При рестарте должны быть повторно
    обработаны максимум сообщения последнего незавершённого пакета — первые
    50 000 (уже зафлушенные) запрашиваться повторно НЕ должны."""
    total = 100_000
    entity = _FakeEntity(-100801, "Big group")
    group = Group(id=-100801, username=None, title="Big group")
    messages = _make_big_history(total)

    db_path = tmp_path / "users.db"
    repository, state_repository = _open_repositories(db_path)
    try:
        crashing_client = _CrashingFakeClient(entity, messages, crash_after=57_000)
        with pytest.raises(_SimulatedCrash):
            await backfill_car_numbers(crashing_client, [group], repository, state_repository)

        checkpoint = state_repository.get(-100801)
        assert checkpoint is not None
        assert checkpoint.completed is False
        # Последний УСПЕШНЫЙ flush — на 50 000-м сообщении (5 полных
        # пакетов по 10 000); 7 000 сообщений после него потеряны, как и
        # должно быть при настоящем обрыве процесса.
        assert checkpoint.last_message_id == total - 50_000 + 1  # id 50001

        resumed_client = _FakeClient(entity, messages)
        stats = await backfill_car_numbers(resumed_client, [group], repository, state_repository)

        # Первые ~50 000 сообщений НЕ запрашиваются повторно — resume
        # начинается ровно с сохранённого checkpoint'а.
        assert resumed_client.iter_messages_offsets[0] == total - 50_000 + 1
        # Переобработаны максимум оставшиеся 50 000 (включая 7000 "потерянных"
        # при обрыве) — не вся история заново.
        assert stats.messages_scanned == 50_000

        for user_id in range(1, 6):
            assert repository.get_car_numbers(user_id) == [f"A{user_id:03d}AA77"]

        final_checkpoint = state_repository.get(-100801)
        assert final_checkpoint.completed is True
        assert final_checkpoint.last_message_id == 1
    finally:
        repository.close()
        state_repository.close()


async def test_multiple_sequential_crashes_resume_without_gaps_or_duplicates(tmp_path):
    """start -> 30k -> crash; resume -> ещё 50k (итого 80k) -> crash;
    resume -> добивает до конца. Каждое продвижение сохраняется между
    запусками, ни одно сообщение не пропущено, номера не дублируются."""
    total = 100_000
    entity = _FakeEntity(-100901, "Multi-crash group")
    group = Group(id=-100901, username=None, title="Multi-crash group")
    messages = _make_big_history(total)

    db_path = tmp_path / "users.db"
    repository, state_repository = _open_repositories(db_path)
    try:
        client_1 = _CrashingFakeClient(entity, messages, crash_after=30_000)
        with pytest.raises(_SimulatedCrash):
            await backfill_car_numbers(client_1, [group], repository, state_repository)
        checkpoint_1 = state_repository.get(-100901)
        assert checkpoint_1.completed is False
        assert checkpoint_1.last_message_id == total - 30_000 + 1

        client_2 = _CrashingFakeClient(entity, messages, crash_after=50_000)
        with pytest.raises(_SimulatedCrash):
            await backfill_car_numbers(client_2, [group], repository, state_repository)
        assert client_2.iter_messages_offsets[0] == total - 30_000 + 1
        checkpoint_2 = state_repository.get(-100901)
        assert checkpoint_2.completed is False
        assert checkpoint_2.last_message_id == total - 80_000 + 1

        client_3 = _FakeClient(entity, messages)
        await backfill_car_numbers(client_3, [group], repository, state_repository)
        assert client_3.iter_messages_offsets[0] == total - 80_000 + 1

        final_checkpoint = state_repository.get(-100901)
        assert final_checkpoint.completed is True
        assert final_checkpoint.last_message_id == 1

        for user_id in range(1, 6):
            assert repository.get_car_numbers(user_id) == [f"A{user_id:03d}AA77"]
    finally:
        repository.close()
        state_repository.close()


async def test_completed_group_is_skipped_without_reading_history(tmp_path):
    entity = _FakeEntity(-101301, "Done group")
    group = Group(id=-101301, username=None, title="Done group")
    messages = _make_messages([(1, 1, "А111АА77")])

    db_path = tmp_path / "users.db"
    repository, state_repository = _open_repositories(db_path)
    try:
        state_repository.save_progress(
            group_id=-101301, chat_name="Done group", last_message_id=1, completed=True,
        )

        client = _FakeClient(entity, messages)
        stats = await backfill_car_numbers(client, [group], repository, state_repository)

        assert client.iter_messages_calls == 0
        assert stats.messages_scanned == 0
        assert repository.get_car_numbers(1) == []
    finally:
        repository.close()
        state_repository.close()


async def test_incomplete_group_resumes_from_checkpoint_not_from_scratch(tmp_path):
    entity = _FakeEntity(-101302, "In-progress group")
    group = Group(id=-101302, username=None, title="In-progress group")
    messages = _make_messages([
        (3, 1, "А001АА77"),
        (2, 2, "А002АА77"),
        (1, 3, "А003АА77"),
    ])

    db_path = tmp_path / "users.db"
    repository, state_repository = _open_repositories(db_path)
    try:
        # Симулируем прогресс с предыдущего (прерванного) запуска: первое
        # сообщение (id=3) уже обработано и зафлушено, id=2 и id=1 — ещё нет.
        repository.add_car_numbers(1, ["A001AA77"])
        state_repository.save_progress(
            group_id=-101302, chat_name="In-progress group", last_message_id=3, completed=False,
        )

        client = _FakeClient(entity, messages)
        stats = await backfill_car_numbers(client, [group], repository, state_repository)

        assert client.iter_messages_offsets == [3]
        assert stats.messages_scanned == 2  # только id=2 и id=1, не id=3 заново
        assert repository.get_car_numbers(2) == ["A002AA77"]
        assert repository.get_car_numbers(3) == ["A003AA77"]

        checkpoint = state_repository.get(-101302)
        assert checkpoint.completed is True
        assert checkpoint.last_message_id == 1
    finally:
        repository.close()
        state_repository.close()


async def test_multiple_groups_have_independent_checkpoints(tmp_path):
    entity_a = _FakeEntity(-101401, "Group A")
    entity_b = _FakeEntity(-101402, "Group B")
    group_a = Group(id=-101401, username=None, title="Group A")
    group_b = Group(id=-101402, username=None, title="Group B")

    messages_a = _make_messages([(2, 1, "А111АА77"), (1, 1, "текст без номера")])
    messages_b = _make_messages([(1, 2, "А222АА77")])

    class _TwoGroupClient:
        def iter_messages_for(self, entity):
            return messages_a if entity is entity_a else messages_b

        async def get_entity(self, ident):
            return entity_a if ident == group_a.identifier else entity_b

        def iter_messages(self, entity, offset_id=0, limit=None):
            messages = self.iter_messages_for(entity)

            async def gen():
                for message in messages:
                    if offset_id == 0 or message.id < offset_id:
                        yield message

            return gen()

    db_path = tmp_path / "users.db"
    repository, state_repository = _open_repositories(db_path)
    try:
        await backfill_car_numbers(
            _TwoGroupClient(), [group_a, group_b], repository, state_repository,
        )

        checkpoint_a = state_repository.get(-101401)
        checkpoint_b = state_repository.get(-101402)
        assert checkpoint_a.completed is True
        assert checkpoint_b.completed is True
        assert checkpoint_a.last_message_id == 1
        assert checkpoint_b.last_message_id == 1
        assert checkpoint_a.group_id == -101401
        assert checkpoint_b.group_id == -101402
    finally:
        repository.close()
        state_repository.close()


async def test_full_rerun_after_completed_does_not_change_anything(tmp_path):
    entity = _FakeEntity(-101501, "Rerun group")
    group = Group(id=-101501, username=None, title="Rerun group")
    messages = _make_messages([(2, 1, "А111АА77"), (1, 2, "А222АА77")])

    db_path = tmp_path / "users.db"
    repository, state_repository = _open_repositories(db_path)
    try:
        await backfill_car_numbers(_FakeClient(entity, messages), [group], repository, state_repository)
        numbers_after_first_run = {
            user_id: repository.get_car_numbers(user_id) for user_id in (1, 2)
        }
        checkpoint_after_first_run = state_repository.get(-101501)

        second_client = _FakeClient(entity, messages)
        stats = await backfill_car_numbers(second_client, [group], repository, state_repository)

        assert second_client.iter_messages_calls == 0
        assert stats.messages_scanned == 0
        for user_id in (1, 2):
            assert repository.get_car_numbers(user_id) == numbers_after_first_run[user_id]
        assert state_repository.get(-101501) == checkpoint_after_first_run
    finally:
        repository.close()
        state_repository.close()
