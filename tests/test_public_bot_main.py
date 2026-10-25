"""
Тесты reader/public_bot/main.py — только read_bot_token() (обязан читать
ИСКЛЮЧИТЕЛЬНО GESHTRAFBOT_TOKEN, никогда не логировать значение) и
sanity-импорт модуля. Полный запуск бота (TelegramClient.start) здесь не
тестируется и не может тестироваться — реальный токен по условиям задачи
(Stage 2) не запрашивается и не используется.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from reader.public_bot import main as public_bot_main  # noqa: E402
from reader.settings import ConfigError  # noqa: E402


def test_module_imports_without_side_effects():
    """Импорт reader/public_bot/main.py не должен ничего подключать/
    запускать сам по себе — только определять run()/read_bot_token()."""
    assert hasattr(public_bot_main, "run")
    assert hasattr(public_bot_main, "read_bot_token")
    assert hasattr(public_bot_main, "main")


def test_read_bot_token_raises_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("GESHTRAFBOT_TOKEN", raising=False)
    # load_dotenv() внутри read_bot_token() ищет .env, поднимаясь от cwd —
    # уводим cwd во временную пустую директорию, чтобы тест не зависел от
    # реального .env разработчика на этой машине.
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError):
        public_bot_main.read_bot_token()


def test_read_bot_token_returns_value_without_logging(monkeypatch, caplog, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GESHTRAFBOT_TOKEN", "test-token-value")

    token = public_bot_main.read_bot_token()

    assert token == "test-token-value"
    assert "test-token-value" not in caplog.text


# ---- bot identity switch: @GEShtrafbot -> @ProtocolGEbot (см. audit
# report) — новый, отдельно зарегистрированный bot, не переименование.
# env var GESHTRAFBOT_TOKEN намеренно НЕ переименована (см. requirements) —
# read_bot_token()'s тесты выше это уже покрывают без изменений. ----


def test_bot_username_switched_to_protocolgebot():
    assert public_bot_main._BOT_USERNAME == "ProtocolGEbot"


def test_session_path_uses_new_protocolgebot_file_not_old_geshtrafbot():
    """Явное требование: новая отдельная Telethon session
    data/sessions/protocolgebot — переиспользование старого
    geshtrafbot.session рискует тем, что Telethon сочтёт себя уже
    авторизованным под старой identity и молча проигнорирует новый
    token (см. audit report)."""
    assert public_bot_main._SESSION_PATH.name == "protocolgebot"
    assert public_bot_main._SESSION_PATH.name != "geshtrafbot"


class _FakeFineMonitorSettings:
    def __init__(self, *, collector_api_enabled=False, collector_hmac_secret=None):
        self.collector_api_enabled = collector_api_enabled
        self.collector_hmac_secret = collector_hmac_secret


class _FakeSettings:
    def __init__(self, fine_monitor):
        self.fine_monitor = fine_monitor


def test_validate_collector_api_config_noop_when_disabled():
    settings = _FakeSettings(_FakeFineMonitorSettings(collector_api_enabled=False))
    public_bot_main.validate_collector_api_config(settings, manual_bootstrap_available=False)


def test_validate_collector_api_config_requires_public_feed_architecture():
    settings = _FakeSettings(
        _FakeFineMonitorSettings(collector_api_enabled=True, collector_hmac_secret="s"),
    )
    with pytest.raises(ConfigError, match="public_feed_enabled"):
        public_bot_main.validate_collector_api_config(settings, manual_bootstrap_available=False)


def test_validate_collector_api_config_requires_hmac_secret():
    settings = _FakeSettings(
        _FakeFineMonitorSettings(collector_api_enabled=True, collector_hmac_secret=None),
    )
    with pytest.raises(ConfigError, match="GEORGIA_COLLECTOR_HMAC_SECRET"):
        public_bot_main.validate_collector_api_config(settings, manual_bootstrap_available=True)


def test_validate_collector_api_config_passes_when_fully_configured():
    settings = _FakeSettings(
        _FakeFineMonitorSettings(collector_api_enabled=True, collector_hmac_secret="s"),
    )
    public_bot_main.validate_collector_api_config(settings, manual_bootstrap_available=True)
