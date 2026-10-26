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
    def __init__(
        self, *, collector_api_enabled=False, collector_hmac_secret=None,
        collector_checknow_base_url=None, georgia_checknow_routing="auto",
    ):
        self.collector_api_enabled = collector_api_enabled
        self.collector_hmac_secret = collector_hmac_secret
        self.collector_checknow_base_url = collector_checknow_base_url
        self.georgia_checknow_routing = georgia_checknow_routing


class _FakeSettings:
    def __init__(self, fine_monitor):
        self.fine_monitor = fine_monitor


def test_validate_collector_api_config_noop_when_disabled():
    settings = _FakeSettings(_FakeFineMonitorSettings(collector_api_enabled=False))
    public_bot_main.validate_collector_api_config(settings)


def test_validate_collector_api_config_requires_hmac_secret():
    settings = _FakeSettings(
        _FakeFineMonitorSettings(collector_api_enabled=True, collector_hmac_secret=None),
    )
    with pytest.raises(ConfigError, match="GEORGIA_COLLECTOR_HMAC_SECRET"):
        public_bot_main.validate_collector_api_config(settings)


def test_validate_collector_api_config_passes_when_fully_configured():
    settings = _FakeSettings(
        _FakeFineMonitorSettings(
            collector_api_enabled=True, collector_hmac_secret="s",
            collector_checknow_base_url="http://10.50.0.2:8766",
        ),
    )
    public_bot_main.validate_collector_api_config(settings)


def test_validate_collector_api_config_passes_without_public_feed_enabled():
    """Регрессия для production-инцидента 2026-10-25: collector_api_enabled=
    true САМ ПО СЕБЕ (public_feed_enabled не упоминается вовсе — _FakeFine
    MonitorSettings не имеет такого атрибута) должно быть достаточно —
    validate_collector_api_config() не должен обращаться к public_feed_
    enabled вообще."""
    settings = _FakeSettings(
        _FakeFineMonitorSettings(
            collector_api_enabled=True, collector_hmac_secret="s",
            collector_checknow_base_url="http://10.50.0.2:8766",
        ),
    )
    assert not hasattr(settings.fine_monitor, "public_feed_enabled")
    public_bot_main.validate_collector_api_config(settings)


# ---- "Fix the Georgia public bot Check Now flow" — REDNET stays a
# fallback transport, not a mandatory dependency, see
# reader/fines/rednet_check_now_provider.py module docstring. ----


def test_validate_collector_api_config_requires_checknow_base_url_for_auto():
    settings = _FakeSettings(
        _FakeFineMonitorSettings(
            collector_api_enabled=True, collector_hmac_secret="s",
            georgia_checknow_routing="auto", collector_checknow_base_url=None,
        ),
    )
    with pytest.raises(ConfigError, match="collector_checknow_base_url"):
        public_bot_main.validate_collector_api_config(settings)


def test_validate_collector_api_config_requires_checknow_base_url_for_rednet():
    settings = _FakeSettings(
        _FakeFineMonitorSettings(
            collector_api_enabled=True, collector_hmac_secret="s",
            georgia_checknow_routing="rednet", collector_checknow_base_url=None,
        ),
    )
    with pytest.raises(ConfigError, match="collector_checknow_base_url"):
        public_bot_main.validate_collector_api_config(settings)


def test_validate_collector_api_config_does_not_require_checknow_base_url_for_local():
    """routing="local" never touches REDNET (see build_check_now_provider) —
    requiring collector_checknow_base_url here would be a false dependency."""
    settings = _FakeSettings(
        _FakeFineMonitorSettings(
            collector_api_enabled=True, collector_hmac_secret="s",
            georgia_checknow_routing="local", collector_checknow_base_url=None,
        ),
    )
    public_bot_main.validate_collector_api_config(settings)


class _FakePoliceGeSession:
    """Minimal stand-in — build_check_now_provider() never calls any method
    on it, only wraps it in PoliceGeProvider(...), so identity is all that
    matters here."""


def test_build_check_now_provider_returns_local_when_collector_api_disabled():
    provider = public_bot_main.build_check_now_provider(
        _FakeFineMonitorSettings(collector_api_enabled=False), _FakePoliceGeSession(),
    )
    assert isinstance(provider, public_bot_main.PoliceGeProvider)


def test_build_check_now_provider_returns_local_for_routing_local():
    """Even with collector_api_enabled=True and a rednet_client available,
    routing="local" must never touch REDNET — proves the mode, not just the
    flag, gates behavior."""
    provider = public_bot_main.build_check_now_provider(
        _FakeFineMonitorSettings(
            collector_api_enabled=True, collector_hmac_secret="s",
            georgia_checknow_routing="local", collector_checknow_base_url="http://10.50.0.2:8766",
        ),
        _FakePoliceGeSession(),
        rednet_client=object(),  # must never be touched/used
    )
    assert isinstance(provider, public_bot_main.PoliceGeProvider)


def test_build_check_now_provider_returns_rednet_only_for_routing_rednet():
    import httpx

    provider = public_bot_main.build_check_now_provider(
        _FakeFineMonitorSettings(
            collector_api_enabled=True, collector_hmac_secret="s",
            georgia_checknow_routing="rednet", collector_checknow_base_url="http://10.50.0.2:8766",
        ),
        _FakePoliceGeSession(),
        rednet_client=httpx.AsyncClient(base_url="http://10.50.0.2:8766"),
    )
    assert isinstance(provider, public_bot_main.RednetCheckNowProvider)


def test_build_check_now_provider_returns_auto_failover_by_default():
    import httpx

    provider = public_bot_main.build_check_now_provider(
        _FakeFineMonitorSettings(
            collector_api_enabled=True, collector_hmac_secret="s",
            georgia_checknow_routing="auto", collector_checknow_base_url="http://10.50.0.2:8766",
        ),
        _FakePoliceGeSession(),
        rednet_client=httpx.AsyncClient(base_url="http://10.50.0.2:8766"),
    )
    assert isinstance(provider, public_bot_main.AutoFailoverCheckNowProvider)


def test_build_check_now_provider_raises_without_rednet_client_when_needed():
    with pytest.raises(ValueError):
        public_bot_main.build_check_now_provider(
            _FakeFineMonitorSettings(
                collector_api_enabled=True, collector_hmac_secret="s",
                georgia_checknow_routing="auto", collector_checknow_base_url="http://10.50.0.2:8766",
            ),
            _FakePoliceGeSession(),
            rednet_client=None,
        )


# ---- bootstrap_infrastructure_needed() — вынесено отдельной, напрямую
# тестируемой функцией именно ПОТОМУ, что отсутствие юнит-теста для этого
# ОДНОГО boolean-выражения (раньше инлайн в run()) позволило production-
# инциденту 2026-10-25 остаться незамеченным (см. её докстрок). ----


class _FakeFineMonitorSettingsWithPublicFeed:
    def __init__(self, *, public_feed_enabled, collector_api_enabled):
        self.public_feed_enabled = public_feed_enabled
        self.collector_api_enabled = collector_api_enabled


@pytest.mark.parametrize(
    "public_feed_enabled,collector_api_enabled,expected",
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_bootstrap_infrastructure_needed(public_feed_enabled, collector_api_enabled, expected):
    fine_monitor = _FakeFineMonitorSettingsWithPublicFeed(
        public_feed_enabled=public_feed_enabled, collector_api_enabled=collector_api_enabled,
    )
    assert public_bot_main.bootstrap_infrastructure_needed(fine_monitor) is expected
