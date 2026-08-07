"""
Тест сборки зависимостей reader/inviter/main.py — конкретно то, что
итоговые отчёты инвайтера должны уходить в тот же рабочий чат проекта, что
и найденные лиды (app.lead_forward_to / LEAD_FORWARD_TO), а НЕ в
fine_monitor.notification_chat_ids (отдельный чат для алертов об
оштрафованных авто, см. задачу).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.inviter.main import _build_operator_notifier  # noqa: E402
from reader.settings import load_settings  # noqa: E402

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
  notification_chat_ids:
    - "@fine_operator_chat"
"""


def _write_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(_CONFIG_YAML, encoding="utf-8")
    return config_path


def _set_required_env(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "dummy")
    monkeypatch.setenv("TELEGRAM_PHONE", "+70000000000")
    monkeypatch.setenv("LEAD_FORWARD_TO", "@lead_chat")


def test_operator_notifier_uses_lead_forward_to_not_fine_monitor_chat(tmp_path, monkeypatch):
    """fine_monitor.notification_chat_ids задан и отличается от
    app.lead_forward_to — отчёты инвайтера должны уйти именно в чат лидов,
    а не подхватить чат fine_monitor (в отличие от
    reader.main.resolve_notification_chat_ids(), который инвайтер больше
    не использует для этой цели)."""
    _set_required_env(monkeypatch)
    config_path = _write_config(tmp_path)

    settings = load_settings(config_path)
    assert settings.fine_monitor.notification_chat_ids == ["fine_operator_chat"]
    assert settings.app.lead_forward_to == ["lead_chat"]

    notifier = _build_operator_notifier(settings)

    assert notifier._chat_ids == ["lead_chat"]
    # session_path передан явно — нужен OperatorNotifier.start() для
    # понятного сообщения, если session_path_notifier не авторизован
    # (см. reader/notifications/authorize_notifier.py и задачу про
    # "Получатель уведомлений оператора ... не найден").
    assert notifier._session_path == settings.telegram.session_path_notifier


def test_operator_notifier_has_no_recipients_when_lead_forward_to_is_empty(tmp_path, monkeypatch):
    """Если LEAD_FORWARD_TO не задан — получателей у отчётов инвайтера
    нет вовсе, даже если fine_monitor.notification_chat_ids настроен: это
    больше не запасной вариант для инвайтера (см. задачу)."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LEAD_FORWARD_TO", "")
    config_path = _write_config(tmp_path)

    settings = load_settings(config_path)
    assert settings.app.lead_forward_to == []

    notifier = _build_operator_notifier(settings)

    assert notifier._chat_ids == []
