"""
Интеграционный тест сборки контейнера зависимостей мониторинга штрафов
(reader/main.py: build_fine_monitor_components/validate_fine_monitor_config).

Ничего реального не запускается: TelegramClient создаётся (это безопасно —
Telethon ничего не подключает в конструкторе, см. test_telegram_source_user_cache.py),
но не .start()'ится; httpx.AsyncClient создаётся, но ни один запрос не
выполняется. Проверяем именно то, что использует reader/main.py — не копию
логики сборки в тесте.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from reader.commands.dispatcher import CommandDispatcher  # noqa: E402
from reader.commands.fine import FineCommand  # noqa: E402
from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.police_ge_provider import PoliceGeProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.jobs.archive_fine_job import ArchiveFineJob  # noqa: E402
from reader.jobs.fine_job import FineJob  # noqa: E402
from reader.jobs.scheduler import Scheduler  # noqa: E402
from reader.main import (  # noqa: E402
    build_fine_monitor_components,
    resolve_allowed_user_ids,
    resolve_notification_chat_ids,
    validate_fine_monitor_config,
)
from reader.notifications.telegram_notification_service import (  # noqa: E402
    TelegramNotificationService,
)
from reader.settings import ConfigError, load_settings  # noqa: E402
from reader.sources.telegram_source import TelegramSource  # noqa: E402
from reader.users.repository import UserRepository  # noqa: E402

_CONFIG_YAML = """
telegram:
  session_name_live: reader_live
  session_name_sync: reader_sync

app:
  log_level: INFO
  groups_file: config/groups.yaml
  scenarios_file: config/scenarios.yaml
  leads_output_file: data/output/leads.jsonl
  users_db_file: data/users.db

fine_monitor:
  enabled: true
  timezone: Asia/Tbilisi
  check_times:
    - "09:00"
    - "15:00"
    - "21:00"
  source_url: https://police.ge/protocol/index.php?lang=en
  request_timeout: 30
  notification_chat_ids:
    - "@operator_chat"
  allowed_user_ids:
    - 111
"""

_CONFIG_YAML_NO_NOTIFICATION_CHATS = _CONFIG_YAML.replace(
    'notification_chat_ids:\n    - "@operator_chat"', "notification_chat_ids: []"
)

_CONFIG_YAML_NO_ALLOWED_USERS = _CONFIG_YAML.replace(
    "allowed_user_ids:\n    - 111", "allowed_user_ids: []"
)


def _write_config(tmp_path: Path, content: str) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(content, encoding="utf-8")
    return config_path


def _set_required_env(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "dummy")
    monkeypatch.setenv("TELEGRAM_PHONE", "+70000000000")
    # Пустая строка, а не delenv(): load_settings() вызывает load_dotenv(),
    # который подхватил бы реальный .env проекта (там уже есть
    # LEAD_FORWARD_TO) для отсутствующей переменной — а вот уже
    # существующую (пусть и пустую) не трогает.
    monkeypatch.setenv("LEAD_FORWARD_TO", "")


def test_load_settings_parses_fine_monitor_section(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_path = _write_config(tmp_path, _CONFIG_YAML)

    settings = load_settings(config_path)

    assert settings.fine_monitor.enabled is True
    assert settings.fine_monitor.timezone == "Asia/Tbilisi"
    assert [t.strftime("%H:%M") for t in settings.fine_monitor.check_times] == [
        "09:00",
        "15:00",
        "21:00",
    ]
    assert settings.fine_monitor.source_url == "https://police.ge/protocol/index.php?lang=en"
    assert settings.fine_monitor.request_timeout == 30
    assert settings.fine_monitor.notification_chat_ids == ["operator_chat"]
    assert settings.fine_monitor.allowed_user_ids == [111]

    # _CONFIG_YAML не задаёт архивные ключи — должны применяться defaults,
    # без правки существующего конфига этого теста.
    assert settings.fine_monitor.archive_check_enabled is True
    assert settings.fine_monitor.archive_check_hour == 4
    assert settings.fine_monitor.archive_interval_days == 30
    assert settings.fine_monitor.archive_daily_limit == 200


def test_load_settings_parses_explicit_archive_settings(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_with_archive = _CONFIG_YAML + (
        "  archive_check_enabled: false\n"
        "  archive_check_hour: 5\n"
        "  archive_interval_days: 14\n"
        "  archive_daily_limit: 50\n"
    )
    config_path = _write_config(tmp_path, config_with_archive)

    settings = load_settings(config_path)

    assert settings.fine_monitor.archive_check_enabled is False
    assert settings.fine_monitor.archive_check_hour == 5
    assert settings.fine_monitor.archive_interval_days == 14
    assert settings.fine_monitor.archive_daily_limit == 50


def test_resolve_notification_chat_ids_prefers_explicit_config(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LEAD_FORWARD_TO", "@lead_chat")
    config_path = _write_config(tmp_path, _CONFIG_YAML)

    settings = load_settings(config_path)

    assert resolve_notification_chat_ids(settings) == ["operator_chat"]


def test_resolve_notification_chat_ids_unaffected_by_multiple_lead_forward_to_recipients(
    tmp_path, monkeypatch,
):
    """Расширение LEAD_FORWARD_TO до нескольких получателей лидов (см.
    задачу) не должно фанаутить уведомления Fine Monitor всем им сразу —
    пока fine_monitor.notification_chat_ids задан явно (как в _CONFIG_YAML,
    "@operator_chat"), фоллбэк на app.lead_forward_to не срабатывает,
    независимо от того, сколько получателей лидов сконфигурировано."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LEAD_FORWARD_TO", "ali_na_l_i,alena_ogi,vladimihailov")
    config_path = _write_config(tmp_path, _CONFIG_YAML)

    settings = load_settings(config_path)

    assert settings.app.lead_forward_to == ["ali_na_l_i", "alena_ogi", "vladimihailov"]
    assert resolve_notification_chat_ids(settings) == ["operator_chat"]


def test_resolve_notification_chat_ids_falls_back_to_lead_forward_to(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LEAD_FORWARD_TO", "@lead_chat")
    config_path = _write_config(tmp_path, _CONFIG_YAML_NO_NOTIFICATION_CHATS)

    settings = load_settings(config_path)

    assert settings.fine_monitor.notification_chat_ids == []
    assert resolve_notification_chat_ids(settings) == ["lead_chat"]


async def test_build_fine_monitor_components_wires_dependencies_correctly(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_path = _write_config(tmp_path, _CONFIG_YAML)
    settings = load_settings(config_path)

    user_repository = UserRepository(tmp_path / "users.db")
    source = TelegramSource(settings.telegram, groups=[], user_repository=user_repository)

    task_repository = FineMonitoringTaskRepository(tmp_path / "users.db")
    detected_fine_repository = DetectedFineRepository(tmp_path / "users.db")

    # Реальный httpx.AsyncClient — конструктор ничего не подключает, ни
    # одного запроса в этом тесте не выполняется.
    http_client = httpx.AsyncClient()

    try:
        notification_chat_ids = resolve_notification_chat_ids(settings)

        fine_job, scheduler, notification_service, command_dispatcher, fine_command = (
            build_fine_monitor_components(
                settings, source, task_repository, detected_fine_repository,
                http_client, notification_chat_ids, settings.fine_monitor.allowed_user_ids,
                user_repository=user_repository,
            )
        )

        assert isinstance(fine_job, FineJob)
        assert isinstance(scheduler, Scheduler)
        assert isinstance(notification_service, TelegramNotificationService)
        assert isinstance(command_dispatcher, CommandDispatcher)
        assert isinstance(fine_command, FineCommand)

        # Один и тот же TelegramClient переиспользован — второе подключение
        # к Telegram не создано.
        assert notification_service._client is source.client
        assert command_dispatcher._client is source.client

        # Repository — те же самые объекты, что переданы на вход, а
        # не пересозданные копии.
        assert fine_job._task_repository is task_repository
        assert isinstance(fine_job._notification_coordinator, FineNotificationCoordinator)
        assert fine_job._notification_coordinator._detected_fine_repository is detected_fine_repository
        assert fine_job._notification_coordinator._notification_service is notification_service
        # UserRepository переиспользован (тот же, что у Pipeline/TelegramSource),
        # второе соединение с users.db не открыто — нужен координатору только
        # чтобы уведомление о новом штрафе показывало Telegram-пользователя.
        assert fine_job._notification_coordinator._user_repository is user_repository

        # FineCheckService получил именно PoliceGeProvider, а не заглушку, и
        # это тот же самый объект, что использует и FineCommand.fine_check.
        assert isinstance(fine_job._check_service, FineCheckService)
        assert isinstance(fine_job._check_service._provider, PoliceGeProvider)
        assert fine_command._check_service is fine_job._check_service
        assert fine_command._notification_coordinator is fine_job._notification_coordinator
        # Тот же UserRepository, что и у FineNotificationCoordinator — нужен
        # "fine check" для строки "Telegram: ..." в результате.
        assert fine_command._user_repository is user_repository

        # Scheduler получил оба job'а — FineJob и ArchiveFineJob, тот же
        # task_repository/check_service/notification_coordinator у обоих
        # (ни второго обращения к police.ge, ни второго notification flow).
        assert len(scheduler._jobs) == 2
        assert scheduler._jobs[0] is fine_job
        archive_fine_job = scheduler._jobs[1]
        assert isinstance(archive_fine_job, ArchiveFineJob)
        assert archive_fine_job._task_repository is task_repository
        assert archive_fine_job._check_service is fine_job._check_service
        assert archive_fine_job._notification_coordinator is fine_job._notification_coordinator

        # Расписание/таймзона взяты из конфига, а не захардкожены.
        assert [t.strftime("%H:%M") for t in fine_job._run_times] == ["09:00", "15:00", "21:00"]
        assert str(fine_job._tz) == "Asia/Tbilisi"

        # Настройки архивного режима — тоже из конфига (см. FineMonitorSettings
        # defaults и _CONFIG_YAML выше, где эти ключи не заданы явно).
        assert fine_job._archive_check_enabled is True
        assert fine_job._archive_check_hour == 4
        assert fine_job._archive_interval_days == 30
        assert archive_fine_job._enabled is True
        assert archive_fine_job._hour == 4
        assert archive_fine_job._interval_days == 30
        assert archive_fine_job._daily_limit == 200
        assert str(archive_fine_job._tz) == "Asia/Tbilisi"

        # CommandDispatcher слушает первый из настроенных чатов уведомлений.
        assert command_dispatcher._chat_id == "operator_chat"
        assert command_dispatcher._allowed_user_ids == {111}

        # FineCommand собран, но ещё не зарегистрирован в build_fine_monitor_components —
        # регистрация делается отдельно, в _run_fine_monitor.
        assert command_dispatcher._commands == {}

        command_dispatcher.register(fine_command)
        assert command_dispatcher._commands == {"fine": fine_command}
    finally:
        user_repository.close()
        task_repository.close()
        detected_fine_repository.close()
        await http_client.aclose()


def test_validate_fine_monitor_config_passes_with_valid_settings(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_path = _write_config(tmp_path, _CONFIG_YAML)
    settings = load_settings(config_path)

    validate_fine_monitor_config(settings, resolve_notification_chat_ids(settings))


def test_validate_fine_monitor_config_fails_without_notification_chat(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_path = _write_config(tmp_path, _CONFIG_YAML_NO_NOTIFICATION_CHATS)
    settings = load_settings(config_path)

    with pytest.raises(ConfigError):
        validate_fine_monitor_config(settings, resolve_notification_chat_ids(settings))


def test_validate_fine_monitor_config_fails_with_empty_allowed_user_ids(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_path = _write_config(tmp_path, _CONFIG_YAML_NO_ALLOWED_USERS)
    settings = load_settings(config_path)

    with pytest.raises(ConfigError, match="allowed_user_ids"):
        validate_fine_monitor_config(settings, resolve_notification_chat_ids(settings))


def test_resolve_allowed_user_ids_returns_configured_list(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_path = _write_config(tmp_path, _CONFIG_YAML)
    settings = load_settings(config_path)

    assert resolve_allowed_user_ids(settings) == [111]


def test_resolve_allowed_user_ids_returns_empty_list_without_fallback(tmp_path, monkeypatch):
    # Автофоллбэк на client.get_me().id убран — пустой список остаётся
    # пустым, никакого обращения к Telegram-клиенту здесь больше нет.
    _set_required_env(monkeypatch)
    config_path = _write_config(tmp_path, _CONFIG_YAML_NO_ALLOWED_USERS)
    settings = load_settings(config_path)

    assert resolve_allowed_user_ids(settings) == []
