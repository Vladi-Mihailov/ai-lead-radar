"""
Интеграционный тест подсистемы локального кэша пользователей Telegram.

Сценарий: в SQLite уже есть пользователь 123 с username "ivan". Приходит
новое сообщение, для которого Telegram смог определить только sender_id,
но не username (например, аккаунт временно недоступен для резолва). Итоговое
сообщение, полученное через публичный интерфейс TelegramSource (тот же,
которым пользуется Pipeline), должно содержать "ivan", а не "123".

Реальные запросы к Telegram не выполняются: событие Telethon подменяется
легковесной фейковой реализацией, а TelegramClient используется только как
неподключённый объект (session-файл создаётся локально, сети не касается).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.settings import TelegramSettings  # noqa: E402
from reader.sources.telegram_source import TelegramSource  # noqa: E402
from reader.users.models import TelegramUserInfo  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402


class _FakeSender:
    """То, что вернул бы Telegram: известен id, но не username."""

    def __init__(self, id, username=None, first_name=None, last_name=None, bot=False):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.bot = bot


class _FakeEvent:
    """Минимальная имитация telethon.events.NewMessage.Event."""

    def __init__(self, *, message_id, chat_id, sender_id, text, date, sender):
        self.id = message_id
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_text = text
        self.date = date
        self._sender = sender

    async def get_sender(self):
        return self._sender


async def test_message_uses_cached_username_when_telegram_omits_it(tmp_path):
    # 1. Локальная БД: пользователь 123 уже известен как "ivan"
    repository = UserRepository(tmp_path / "users.db")
    repository.upsert(
        TelegramUserInfo(user_id=123, username="ivan", first_name=None, last_name=None)
    )

    telegram_settings = TelegramSettings(
        session_path_live=tmp_path / "session_live",
        session_path_sync=tmp_path / "session_sync",
        api_id=1,
        api_hash="dummy",
        phone="+70000000000",
    )
    source = TelegramSource(telegram_settings, groups=[], user_repository=repository)

    try:
        # 2. Новое сообщение: Telegram отдал только sender_id=123, username не вернул
        event = _FakeEvent(
            message_id=555,
            chat_id=-100123456789,
            sender_id=123,
            text="Нужно оформить осаго",
            date=datetime.now(timezone.utc),
            sender=_FakeSender(id=123, username=None),
        )

        await source.handle_new_message(event)

        # 3. Читаем результат через тот же публичный интерфейс, что и Pipeline
        messages_iter = source.messages()
        try:
            message = await anext(messages_iter)
        finally:
            await messages_iter.aclose()

        assert message.sender_id == 123
        assert message.sender_username == "ivan"
        assert message.sender_username != "123"
    finally:
        repository.close()
