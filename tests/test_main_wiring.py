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

from reader.checkout.lock_repository import CheckoutLockRepository  # noqa: E402
from reader.checkout.payment_gateway import (  # noqa: E402
    CardSecrets,
    PlaywrightBankGatewayClient,
    PlaywrightBrowserLauncher,
)
from reader.checkout.personal_info import OcrPersonalInfoProvider  # noqa: E402
from reader.checkout.reference_data import TplReferenceDataClient  # noqa: E402
from reader.checkout.service import CheckoutService  # noqa: E402
from reader.checkout.telegram_integration import CheckoutReplyHandler  # noqa: E402
from reader.checkout.tpl_client import TplGeClient  # noqa: E402
from reader.commands.album_collector import AlbumCollector  # noqa: E402
from reader.commands.dispatcher import CommandDispatcher  # noqa: E402
from reader.commands.fine import FineCommand  # noqa: E402
from reader.commands.insurance_ocr import InsuranceOcrCommand  # noqa: E402
from reader.fines.check_service import FineCheckService  # noqa: E402
from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.notification_coordinator import FineNotificationCoordinator  # noqa: E402
from reader.fines.police_ge_provider import PoliceGeProvider  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.jobs.archive_fine_job import ArchiveFineJob  # noqa: E402
from reader.jobs.fine_job import FineJob  # noqa: E402
from reader.jobs.scheduler import Scheduler  # noqa: E402
from reader.main import (  # noqa: E402
    build_checkout_components,
    build_fine_monitor_components,
    build_insurance_ocr_components,
    build_lead_ai_sink,
    is_checkout_configured,
    resolve_allowed_user_ids,
    resolve_notification_chat_ids,
    resolve_telegram_sink_recipients,
    validate_fine_monitor_config,
)
from reader.notifications.telegram_notification_service import (  # noqa: E402
    TelegramNotificationService,
)
from reader.ocr.service import OcrService  # noqa: E402
from reader.settings import ConfigError, load_settings  # noqa: E402
from reader.sinks.lead_ai_sink import LeadAiSink  # noqa: E402
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
        # fine-команда НЕ затронута задачей про Insurance OCR — допуск по
        # sender_id для неё остаётся включённым (см. reader/main.py::
        # build_insurance_ocr_components, где restrict_to_allowed_users=False
        # передаётся ТОЛЬКО для инстанса CommandDispatcher чата OCR).
        assert command_dispatcher._restrict_to_allowed_users is True

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


# ---- ocr (insurance ocr) ----


def test_load_settings_defaults_ocr_section(tmp_path, monkeypatch):
    """config.yaml без секции ocr вовсе, .env без OPENAI_API_KEY — команда
    просто не должна быть настроена (см. reader/main.py::run() — она в
    этом случае не поднимается вовсе, как и TelegramSink при пустом
    app.lead_forward_to)."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    config_path = _write_config(tmp_path, _CONFIG_YAML)

    settings = load_settings(config_path)

    assert settings.ocr.openai_api_key is None
    assert settings.ocr.service_chat_id is None
    assert settings.ocr.allowed_user_ids == []
    assert settings.ocr.vision_model == "gpt-5-mini"
    assert settings.ocr.album_debounce_seconds == 1.5


def test_load_settings_parses_explicit_ocr_section(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    config_with_ocr = _CONFIG_YAML + (
        "\nocr:\n"
        '  service_chat_id: "@insurance_ocr_service_chat"\n'
        "  allowed_user_ids:\n"
        "    - 222\n"
        "  vision_model: gpt-5-mini\n"
        "  album_debounce_seconds: 2.5\n"
    )
    config_path = _write_config(tmp_path, config_with_ocr)

    settings = load_settings(config_path)

    assert settings.ocr.openai_api_key == "test-key-not-real"
    assert settings.ocr.service_chat_id == "insurance_ocr_service_chat"
    assert settings.ocr.allowed_user_ids == [222]
    assert settings.ocr.vision_model == "gpt-5-mini"
    assert settings.ocr.album_debounce_seconds == 2.5


def test_load_settings_parses_numeric_ocr_service_chat_id(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    config_with_ocr = _CONFIG_YAML + (
        "\nocr:\n"
        "  service_chat_id: -1009876543210\n"
        "  allowed_user_ids:\n"
        "    - 222\n"
    )
    config_path = _write_config(tmp_path, config_with_ocr)

    settings = load_settings(config_path)

    assert settings.ocr.service_chat_id == -1009876543210


async def test_build_insurance_ocr_components_wires_dependencies_correctly(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    config_with_ocr = _CONFIG_YAML + (
        "\nocr:\n"
        '  service_chat_id: "@insurance_ocr_service_chat"\n'
        "  allowed_user_ids:\n"
        "    - 222\n"
    )
    config_path = _write_config(tmp_path, config_with_ocr)
    settings = load_settings(config_path)

    user_repository = UserRepository(tmp_path / "users.db")
    source = TelegramSource(settings.telegram, groups=[], user_repository=user_repository)

    try:
        command_dispatcher, insurance_command, album_collector = build_insurance_ocr_components(
            settings, source,
        )

        assert isinstance(command_dispatcher, CommandDispatcher)
        assert isinstance(insurance_command, InsuranceOcrCommand)
        assert isinstance(album_collector, AlbumCollector)
        assert isinstance(insurance_command._ocr_service, OcrService)

        # checkout не настроен в этом конфиге — Email/Телефон по умолчанию
        # для Telegram-черновика тоже не заданы (см. reader/settings.py::
        # CheckoutSettings).
        assert insurance_command._default_email is None
        assert insurance_command._default_phone is None

        # Тот же TelegramClient, что и у остального Reader — второе
        # подключение к Telegram не создано (см. задачу).
        assert command_dispatcher._client is source.client

        # Свой, отдельный от fine_monitor, чат/список операторов.
        assert command_dispatcher._chat_id == "insurance_ocr_service_chat"
        assert command_dispatcher._allowed_user_ids == {222}
        # Допуск к самому OCR больше не ограничен конкретным отправителем
        # (см. задачу) — любой участник настроенного чата. allowed_user_ids
        # всё ещё передаётся (используется дальше для checkout, см.
        # build_checkout_components), но restrict_to_allowed_users=False
        # отключает проверку sender_id именно для этого инстанса.
        assert command_dispatcher._restrict_to_allowed_users is False

        # InsuranceOcrCommand больше не принимает allowed_user_ids вовсе —
        # допуск к OCR определяется только чатом.
        assert not hasattr(insurance_command, "_allowed_user_ids")

        # AlbumCollector зовёт именно InsuranceOcrCommand.handle_album — не
        # копию/дублирующую бизнес-логику.
        assert album_collector._on_group_ready == insurance_command.handle_album
    finally:
        user_repository.close()


async def test_build_insurance_ocr_components_wires_checkout_contact_defaults(tmp_path, monkeypatch):
    """Email/Телефон checkout settings попадают в InsuranceOcrCommand как
    default_email/default_phone для Telegram-черновика "Распознано: ..."
    (см. reader/commands/insurance_ocr.py)."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    config = _CONFIG_YAML + (
        "\nocr:\n"
        '  service_chat_id: "@insurance_ocr_service_chat"\n'
        "  allowed_user_ids:\n"
        "    - 222\n"
        "\ncheckout:\n"
        "  payment_bank: bank_of_georgia\n"
        "  policy_period: 30-D\n"
        '  phone: "925000000000"\n'
        '  email: "tplgee@mail.ru"\n'
    )
    config_path = _write_config(tmp_path, config)
    settings = load_settings(config_path)

    user_repository = UserRepository(tmp_path / "users.db")
    source = TelegramSource(settings.telegram, groups=[], user_repository=user_repository)

    try:
        _dispatcher, insurance_command, _albums = build_insurance_ocr_components(settings, source)

        assert insurance_command._default_email == "tplgee@mail.ru"
        assert insurance_command._default_phone == "925000000000"
    finally:
        user_repository.close()


# ---- checkout (tpl.ge) ----


def test_load_settings_defaults_checkout_section(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_path = _write_config(tmp_path, _CONFIG_YAML)

    settings = load_settings(config_path)

    assert settings.checkout.payment_bank is None
    assert settings.checkout.policy_period is None
    assert settings.checkout.phone is None
    assert settings.checkout.email is None
    assert is_checkout_configured(settings) is False


def test_load_settings_parses_explicit_checkout_section(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_with_checkout = _CONFIG_YAML + (
        "\ncheckout:\n"
        "  payment_bank: bank_of_georgia\n"
        "  policy_period: 30-D\n"
        '  phone: "925000000000"\n'
        '  email: "tplgee@mail.ru"\n'
    )
    config_path = _write_config(tmp_path, config_with_checkout)

    settings = load_settings(config_path)

    assert settings.checkout.payment_bank == "bank_of_georgia"
    assert settings.checkout.policy_period == "30-D"
    assert settings.checkout.phone == "925000000000"
    assert settings.checkout.email == "tplgee@mail.ru"
    assert is_checkout_configured(settings) is True


def test_load_settings_rejects_unsupported_payment_bank(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_with_checkout = _CONFIG_YAML + (
        "\ncheckout:\n"
        "  payment_bank: liberty_bank\n"  # не подтверждено research'ом
        "  policy_period: 30-D\n"
        '  phone: "925000000000"\n'
        '  email: "tplgee@mail.ru"\n'
    )
    config_path = _write_config(tmp_path, config_with_checkout)

    with pytest.raises(ConfigError, match="payment_bank"):
        load_settings(config_path)


def test_load_settings_rejects_malformed_policy_period(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_with_checkout = _CONFIG_YAML + (
        "\ncheckout:\n"
        "  payment_bank: bank_of_georgia\n"
        "  policy_period: one-month\n"
        '  phone: "925000000000"\n'
        '  email: "tplgee@mail.ru"\n'
    )
    config_path = _write_config(tmp_path, config_with_checkout)

    with pytest.raises(ConfigError, match="policy_period"):
        load_settings(config_path)


def test_load_settings_legacy_payment_bank_alone_does_not_fail_fast_and_does_not_enable_checkout(
    tmp_path, monkeypatch,
):
    """payment_bank/policy_period — LEGACY (см. reader/settings.py::
    CheckoutSettings): банк-эквайер и период полиса теперь поля конкретной
    Telegram-заявки, а не runtime-настройка, поэтому payment_bank, заданный
    в одиночку (без phone/email), больше НЕ считается "checkout включён" и
    не требует policy_period/phone/email — в отличие от старого поведения
    (см. git history), где payment_bank один запускал fail-fast."""
    _set_required_env(monkeypatch)
    only_bank = _CONFIG_YAML + "\ncheckout:\n  payment_bank: bank_of_georgia\n"
    settings = load_settings(_write_config(tmp_path, only_bank))

    assert settings.checkout.payment_bank == "bank_of_georgia"
    assert is_checkout_configured(settings) is False


def test_load_settings_fails_fast_when_phone_or_email_missing(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    missing_email = _CONFIG_YAML + (
        "\ncheckout:\n"
        "  payment_bank: bank_of_georgia\n"
        "  policy_period: 30-D\n"
        '  phone: "925000000000"\n'
    )
    with pytest.raises(ConfigError, match="email"):
        load_settings(_write_config(tmp_path, missing_email))


async def test_build_checkout_components_wires_dependencies_correctly(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    config_with_all = _CONFIG_YAML + (
        "\nocr:\n"
        '  service_chat_id: "@insurance_ocr_service_chat"\n'
        "  allowed_user_ids:\n"
        "    - 222\n"
        "\ncheckout:\n"
        "  payment_bank: bank_of_georgia\n"
        "  policy_period: 30-D\n"
        '  phone: "925000000000"\n'
        '  email: "tplgee@mail.ru"\n'
    )
    config_path = _write_config(tmp_path, config_with_all)
    settings = load_settings(config_path)

    user_repository = UserRepository(tmp_path / "users.db")
    source = TelegramSource(settings.telegram, groups=[], user_repository=user_repository)
    http_client = httpx.AsyncClient()

    fake_card_secrets = CardSecrets(card_number="4111", expiry_month="12", expiry_year="30", cvv="123")
    handler = None

    try:
        handler = build_checkout_components(settings, source, http_client, card_secrets=fake_card_secrets)

        assert isinstance(handler, CheckoutReplyHandler)
        assert isinstance(handler._service, CheckoutService)
        assert isinstance(handler._service._tpl_client, TplGeClient)
        assert isinstance(handler._service._reference_data, TplReferenceDataClient)
        # payment_bank/policy_period больше НЕ конструкторные атрибуты
        # CheckoutService (см. reader/checkout/service.py) — это поля
        # конкретной Telegram-заявки, а не runtime-настройка.
        assert not hasattr(handler._service, "_payment_bank")
        assert not hasattr(handler._service, "_policy_period")

        # Реальный OcrPersonalInfoProvider, а не fail-closed default, с
        # phone/email именно из config.yaml (см. reader/main.py::
        # build_checkout_components и задачу: "Checkout service должен
        # получать их через settings/dependency injection").
        personal_info_provider = handler._service._personal_info_provider
        assert isinstance(personal_info_provider, OcrPersonalInfoProvider)
        assert personal_info_provider._phone == "925000000000"
        assert personal_info_provider._email == "tplgee@mail.ru"
        assert personal_info_provider._reference_data is handler._service._reference_data

        # Реальный банковский gateway (Playwright boundary), не
        # NotImplementedBankGatewayClient — карта из переданного card_secrets
        # (в реальном запуске — CardSecrets.load() из окружения, см.
        # reader/main.py::_run_insurance_ocr).
        bank_gateway = handler._service._bank_gateway
        assert isinstance(bank_gateway, PlaywrightBankGatewayClient)
        assert bank_gateway._card_secrets is fake_card_secrets
        assert isinstance(bank_gateway._launcher, PlaywrightBrowserLauncher)

        # Lock repository — тот же users_db_file, что и у остальных
        # repository проекта (см. reader/checkout/lock_repository.py про
        # restart/recovery).
        assert isinstance(handler._service._lock_repository, CheckoutLockRepository)

        # CheckoutReplyHandler больше НЕ хранит allowed_user_ids — допуск к
        # reply/pay/OTP определяется только чатом (см. задачу:
        # production показал, что sender_id вне ocr.allowed_user_ids молча
        # игнорировался даже в правильном чате). Ownership конкретного
        # checkout определяется operator_user_id заявки, не общим списком.
        assert not hasattr(handler, "_allowed_user_ids")
    finally:
        if handler is not None:
            handler._service._lock_repository.close()
        user_repository.close()
        await http_client.aclose()


# ---- lead_ai (AI-анализ лида, только для настроенного recipient) ----


def test_load_settings_defaults_lead_ai_section(tmp_path, monkeypatch):
    """config.yaml без секции lead_ai вовсе — AI-анализ должен быть
    полностью выключен по умолчанию (см. reader/settings.py::LeadAiSettings),
    как и остальные опциональные интеграции проекта."""
    _set_required_env(monkeypatch)
    config_path = _write_config(tmp_path, _CONFIG_YAML)

    settings = load_settings(config_path)

    assert settings.lead_ai.enabled is False
    assert settings.lead_ai.recipient is None
    assert settings.lead_ai.model == "gpt-5-mini"


def test_load_settings_parses_explicit_lead_ai_section(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_with_lead_ai = _CONFIG_YAML + (
        "\nlead_ai:\n"
        "  enabled: true\n"
        '  recipient: "@alena_ogi"\n'
        "  model: gpt-5-mini\n"
    )
    config_path = _write_config(tmp_path, config_with_lead_ai)

    settings = load_settings(config_path)

    assert settings.lead_ai.enabled is True
    assert settings.lead_ai.recipient == "alena_ogi"
    assert settings.lead_ai.model == "gpt-5-mini"


def test_load_settings_parses_numeric_lead_ai_recipient(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    config_with_lead_ai = _CONFIG_YAML + (
        "\nlead_ai:\n"
        "  enabled: true\n"
        "  recipient: -1009876543210\n"
    )
    config_path = _write_config(tmp_path, config_with_lead_ai)

    settings = load_settings(config_path)

    assert settings.lead_ai.recipient == -1009876543210


def _lead_ai_source(tmp_path, settings, user_repository) -> TelegramSource:
    return TelegramSource(settings.telegram, groups=[], user_repository=user_repository)


def test_build_lead_ai_sink_returns_none_when_disabled(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    config_path = _write_config(tmp_path, _CONFIG_YAML)
    settings = load_settings(config_path)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _lead_ai_source(tmp_path, settings, user_repository)
        assert build_lead_ai_sink(settings, source) is None
    finally:
        user_repository.close()


def test_build_lead_ai_sink_returns_none_when_recipient_missing(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    config_with_lead_ai = _CONFIG_YAML + "\nlead_ai:\n  enabled: true\n"
    config_path = _write_config(tmp_path, config_with_lead_ai)
    settings = load_settings(config_path)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _lead_ai_source(tmp_path, settings, user_repository)
        assert build_lead_ai_sink(settings, source) is None
    finally:
        user_repository.close()


def test_build_lead_ai_sink_returns_none_when_openai_api_key_missing(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    config_with_lead_ai = _CONFIG_YAML + (
        "\nlead_ai:\n  enabled: true\n  recipient: \"@alena_ogi\"\n"
    )
    config_path = _write_config(tmp_path, config_with_lead_ai)
    settings = load_settings(config_path)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _lead_ai_source(tmp_path, settings, user_repository)
        assert build_lead_ai_sink(settings, source) is None
    finally:
        user_repository.close()


def test_build_lead_ai_sink_wires_configured_recipient_and_model(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    config_with_lead_ai = _CONFIG_YAML + (
        "\nlead_ai:\n"
        "  enabled: true\n"
        '  recipient: "@alena_ogi"\n'
        "  model: gpt-5-mini\n"
    )
    config_path = _write_config(tmp_path, config_with_lead_ai)
    settings = load_settings(config_path)

    user_repository = UserRepository(tmp_path / "users.db")
    try:
        source = _lead_ai_source(tmp_path, settings, user_repository)
        sink = build_lead_ai_sink(settings, source)

        assert isinstance(sink, LeadAiSink)
        assert sink._recipient == "alena_ogi"
        assert sink._service._model == "gpt-5-mini"
        # Тот же TelegramClient, что и у остального Reader — второе
        # подключение к Telegram не создано.
        assert sink._client is source.client
    finally:
        user_repository.close()


# ---- resolve_telegram_sink_recipients: получатель lead_ai исключается из
# обычной пересылки TelegramSink (см. задачу — AI должен решить ДО первой
# отправки этому получателю, обычный TelegramSink его больше не касается) ----


def test_resolve_telegram_sink_recipients_excludes_lead_ai_recipient():
    result = resolve_telegram_sink_recipients(
        ["ali_na_l_i", "alena_ogi", "alenaogir"], "alena_ogi",
    )

    assert result == ["ali_na_l_i", "alenaogir"]


def test_resolve_telegram_sink_recipients_is_case_insensitive_for_usernames():
    """Telegram username'ы регистронезависимы (см. TelegramSink.start(),
    та же дедупликация "один и тот же аккаунт" — только там уже после
    резолва в entity.id, а не по сырой строке, как здесь)."""
    result = resolve_telegram_sink_recipients(
        ["ali_na_l_i", "ALENA_OGI", "alenaogir"], "alena_ogi",
    )

    assert result == ["ali_na_l_i", "alenaogir"]


def test_resolve_telegram_sink_recipients_matches_numeric_recipient():
    result = resolve_telegram_sink_recipients(
        ["ali_na_l_i", -1009876543210, "alenaogir"], -1009876543210,
    )

    assert result == ["ali_na_l_i", "alenaogir"]


def test_resolve_telegram_sink_recipients_returns_list_unchanged_when_lead_ai_recipient_is_none():
    """lead_ai_recipient=None — вызывающий код (run()) передаёт None именно
    когда LeadAiSink не поднят (lead_ai.enabled=false либо не настроен) —
    тогда список получателей TelegramSink не должен как-либо меняться."""
    forward_to = ["ali_na_l_i", "alena_ogi", "alenaogir"]

    result = resolve_telegram_sink_recipients(forward_to, None)

    assert result == forward_to


def test_resolve_telegram_sink_recipients_is_noop_when_recipient_not_in_list():
    """lead_ai.recipient не входит в app.lead_forward_to вовсе — фильтрация
    ничего не меняет (edge case конфигурации, не должен ронять wiring)."""
    result = resolve_telegram_sink_recipients(
        ["ali_na_l_i", "alenaogir"], "someone_else",
    )

    assert result == ["ali_na_l_i", "alenaogir"]
