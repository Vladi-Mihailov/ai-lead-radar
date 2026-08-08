"""
Тесты Pipeline — побочное обновление UserRepository.keywords по совпавшим
сценариям (см. reader/users/keyword_matches.py) и UserRepository.car_numbers
по тексту любого сообщения, независимо от совпадения сценария (см.
reader/users/car_numbers.py). Проверяем, что существующее поведение (lead
detection/forwarding в sinks) не меняется, а оба обновления — чистые
side-effect'ы поверх него.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.core.engine import MatchEngine  # noqa: E402
from reader.core.models import Message  # noqa: E402
from reader.core.pipeline import Pipeline  # noqa: E402
from reader.scenarios import KeywordMatcher, Scenario  # noqa: E402
from reader.sinks.base import BaseSink  # noqa: E402
from reader.sources.base import BaseSource  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_SCENARIOS = [Scenario(name="osago", enabled=True, keywords=("осаго", "страховка"))]


class _FakeSource(BaseSource):
    def __init__(self, messages):
        self._messages_list = messages
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def messages(self):
        for message in self._messages_list:
            yield message

    async def stop(self):
        self.stopped = True


class _FakeSink(BaseSink):
    def __init__(self, fail=False):
        self.handled_events = []
        self._fail = fail

    async def handle(self, event):
        if self._fail:
            raise RuntimeError("симулированный сбой sink")
        self.handled_events.append(event)


def _message(
    message_id=1,
    sender_id=111,
    text="нужна страховка",
    sender_username="ivan",
    sender_name=None,
) -> Message:
    return Message(
        id=message_id,
        chat_id=-100999,
        chat_title="Test group",
        sender_id=sender_id,
        sender_username=sender_username,
        sender_name=sender_name,
        text=text,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        link=None,
    )


def _engine() -> MatchEngine:
    return MatchEngine(KeywordMatcher(_SCENARIOS))


async def test_matched_message_records_keywords_and_still_forwards_to_sink(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource([_message(text="нужна страховка")])
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        assert user_repository.get_keywords(111) == ["страховка"]
        # Существующее поведение (forwarding в sinks) не изменилось.
        assert len(sink.handled_events) == 1
        assert sink.handled_events[0].message.text == "нужна страховка"
    finally:
        user_repository.close()


async def test_unmatched_message_does_not_record_keywords_or_call_sink(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource([_message(text="привет, как дела?")])
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        assert user_repository.get_keywords(111) == []
        assert sink.handled_events == []
    finally:
        user_repository.close()


async def test_repeated_keyword_across_messages_does_not_duplicate(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource(
            [
                _message(message_id=1, text="нужна страховка"),
                _message(message_id=2, text="где оформить страховка"),
            ]
        )
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        assert user_repository.get_keywords(111) == ["страховка"]
        assert len(sink.handled_events) == 2
    finally:
        user_repository.close()


async def test_new_keywords_from_later_message_accumulate(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource(
            [
                _message(message_id=1, text="нужна страховка"),
                _message(message_id=2, text="и осаго тоже"),
            ]
        )
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        assert user_repository.get_keywords(111) == ["страховка", "осаго"]
    finally:
        user_repository.close()


async def test_message_without_sender_id_does_not_break_processing(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource([_message(sender_id=None, text="нужна страховка")])
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        # Никакого падения — сообщение всё равно дошло до sink.
        assert len(sink.handled_events) == 1
    finally:
        user_repository.close()


async def test_user_repository_failure_does_not_break_sink_forwarding(tmp_path, monkeypatch):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        def failing_add_keywords(user_id, keywords):
            raise RuntimeError("симулированный сбой SQLite")

        monkeypatch.setattr(user_repository, "add_keywords", failing_add_keywords)

        sink = _FakeSink()
        source = _FakeSource([_message(text="нужна страховка")])
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        # Сбой обновления keywords не должен мешать доставке лида в sink.
        assert len(sink.handled_events) == 1
    finally:
        user_repository.close()


async def test_sink_failure_still_isolated_as_before(tmp_path):
    """Регрессия: ошибка в sink не должна прерывать обработку — это
    поведение существовало до добавления keywords и не должно измениться."""
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        failing_sink = _FakeSink(fail=True)
        source = _FakeSource([_message(text="нужна страховка")])
        pipeline = Pipeline(source, _engine(), [failing_sink], user_repository)

        await pipeline.run()

        # Несмотря на сбой sink, keywords всё равно должны обновиться.
        assert user_repository.get_keywords(111) == ["страховка"]
    finally:
        user_repository.close()


# ---- car_numbers: тот же side-effect путь, что и keywords, но независимо
# от совпадения сценария (см. reader/users/car_numbers.py) ----


async def test_message_with_car_number_records_it_even_without_keyword_match(tmp_path):
    """В отличие от keywords — госномер сохраняется даже если сообщение НЕ
    совпало ни с одним сценарием (см. задачу: "который пользователь
    упоминал в сообщениях", а не только в тех, что дали ScenarioMatch)."""
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource([_message(text="мой номер А111АА77, продаю")])
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        assert user_repository.get_car_numbers(111) == ["A111AA77"]
        # Сценарий не совпал ("осаго"/"страховка" здесь нет) — sink не
        # вызывается, как и раньше.
        assert sink.handled_events == []
        assert user_repository.get_keywords(111) == []
    finally:
        user_repository.close()


async def test_message_with_both_keyword_and_car_number_records_both(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource([_message(text="нужна страховка, номер А111АА77")])
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        assert user_repository.get_keywords(111) == ["страховка"]
        assert user_repository.get_car_numbers(111) == ["A111AA77"]
        assert len(sink.handled_events) == 1
    finally:
        user_repository.close()


async def test_car_numbers_from_different_messages_accumulate(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource(
            [
                _message(message_id=1, text="А111АА77"),
                _message(message_id=2, text="а ещё Х777ХХ197"),
            ]
        )
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        assert user_repository.get_car_numbers(111) == ["A111AA77", "X777XX197"]
    finally:
        user_repository.close()


async def test_repeated_car_number_across_messages_does_not_duplicate(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource(
            [
                _message(message_id=1, text="А111АА77"),
                _message(message_id=2, text="напомню, A111AA77"),
            ]
        )
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        assert user_repository.get_car_numbers(111) == ["A111AA77"]
    finally:
        user_repository.close()


async def test_message_without_car_number_does_not_record_anything(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource([_message(text="нужна страховка")])
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        assert user_repository.get_car_numbers(111) == []
    finally:
        user_repository.close()


async def test_message_without_sender_id_does_not_break_car_number_processing(tmp_path):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        sink = _FakeSink()
        source = _FakeSource([_message(sender_id=None, text="А111АА77")])
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        # Никакого падения — сообщение всё равно дошло до sink (нет
        # совпадения по keyword, поэтому sink и не должен вызваться, но
        # главное — процесс не падает при sender_id=None).
        assert sink.handled_events == []
    finally:
        user_repository.close()


async def test_car_number_repository_failure_does_not_break_sink_forwarding(tmp_path, monkeypatch):
    user_repository = UserRepository(tmp_path / "users.db")
    try:
        def failing_add_car_numbers(user_id, car_numbers):
            raise RuntimeError("симулированный сбой SQLite")

        monkeypatch.setattr(user_repository, "add_car_numbers", failing_add_car_numbers)

        sink = _FakeSink()
        source = _FakeSource([_message(text="нужна страховка, номер А111АА77")])
        pipeline = Pipeline(source, _engine(), [sink], user_repository)

        await pipeline.run()

        # Сбой обновления car_numbers не должен мешать ни доставке лида в
        # sink, ни обновлению keywords.
        assert len(sink.handled_events) == 1
        assert user_repository.get_keywords(111) == ["страховка"]
    finally:
        user_repository.close()
