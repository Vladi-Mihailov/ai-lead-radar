"""Сборка ЛС-черновиков в reader/main.py и настройки dm_outreach (Phase 2):
без Telegram/OpenAI-запросов, БД — во временном каталоге."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from reader.dm_campaigns.observer import DmOutreachObserver
from reader.dm_campaigns.processor import DmDraftProcessor
from reader.groups import Group
from reader.main import build_dm_outreach_components
from reader.scenarios import load_scenarios
from reader.settings import ConfigError, DmOutreachSettings, load_settings
from reader.sources.telegram_source import _reply_to_msg_id

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
  enabled: false
"""


def _settings(tmp_path, monkeypatch, extra: str = "", *, openai_key: str = ""):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "dummy")
    monkeypatch.setenv("TELEGRAM_PHONE", "+70000000000")
    monkeypatch.setenv("LEAD_FORWARD_TO", "")
    monkeypatch.setenv("OPENAI_API_KEY", openai_key)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.yaml"
    path.write_text(_CONFIG_YAML + extra, encoding="utf-8")
    return load_settings(path)


def test_dm_outreach_defaults_without_config_section(tmp_path, monkeypatch):
    dm = _settings(tmp_path, monkeypatch).dm_outreach
    assert dm == DmOutreachSettings()
    assert (dm.model, dm.context_wait_seconds, dm.recent_message_retention_hours, dm.fresh_context_hours,
            dm.max_context_messages, dm.max_evidence_messages, dm.draft_processor_interval_seconds,
            dm.drafting_recovery_seconds) == ("gpt-5-mini", 180, 48, 3, 5, 15, 30, 600)


def test_dm_outreach_section_parsed(tmp_path, monkeypatch):
    dm = _settings(tmp_path, monkeypatch, "dm_outreach:\n  context_wait_seconds: 60\n  model: gpt-x\n").dm_outreach
    assert (dm.context_wait_seconds, dm.model, dm.fresh_context_hours) == (60, "gpt-x", 3)


def test_dm_outreach_invalid_value_is_config_error(tmp_path, monkeypatch):
    with pytest.raises(ConfigError):
        _settings(tmp_path, monkeypatch, "dm_outreach:\n  context_wait_seconds: -5\n")


def test_components_not_built_without_openai_key(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    scenarios = load_scenarios(PROJECT_ROOT / "config" / "scenarios.yaml")
    assert build_dm_outreach_components(settings, scenarios, []) is None
    assert not (tmp_path / "data" / "users.db").exists()


def test_components_built_with_openai_key(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch, openai_key="sk-test")
    scenarios = load_scenarios(PROJECT_ROOT / "config" / "scenarios.yaml")
    built = build_dm_outreach_components(settings, scenarios, [Group(id=None, username="VerhniyLars", title="Верхний Ларс")])
    assert built is not None
    observer, processor, repositories = built
    try:
        assert isinstance(observer, DmOutreachObserver) and isinstance(processor, DmDraftProcessor)
        # Сразу после сборки — всё выключено: ни кампаний, ни кандидатов.
        campaigns = repositories[0].list_campaigns()
        assert [c.key for c in campaigns] == ["fuel", "border_queue", "insurance"]
        assert not any(c.enabled for c in campaigns)
        assert repositories[1].list_all() == []
    finally:
        for repository in repositories:
            repository.close()


@pytest.mark.parametrize("event,expected", [
    (SimpleNamespace(message=SimpleNamespace(reply_to=SimpleNamespace(reply_to_msg_id=42))), 42),
    (SimpleNamespace(message=SimpleNamespace(reply_to=None)), None),
    (SimpleNamespace(), None),
    (SimpleNamespace(message=SimpleNamespace(reply_to=SimpleNamespace(reply_to_msg_id="x"))), None),
])
def test_reply_to_msg_id_extraction(event, expected):
    assert _reply_to_msg_id(event) == expected


class _Sender:
    def __init__(self, id, username):
        self.id, self.username, self.first_name, self.last_name, self.bot, self.access_hash = id, username, "Ivan", None, False, None


class _Event:
    def __init__(self, *, chat_id, reply_to_msg_id):
        self.id = 77
        self.chat_id = chat_id
        self.sender_id = 555
        self.raw_text = "Что сейчас на Ларсе?"
        self.date = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        self.message = SimpleNamespace(reply_to=SimpleNamespace(reply_to_msg_id=reply_to_msg_id))
        self._sender = _Sender(555, "ivan")

    async def get_sender(self):
        return self._sender


async def test_telegram_source_passes_chat_identifier_and_reply_id(tmp_path):
    from reader.settings import TelegramSettings
    from reader.sources.telegram_source import TelegramSource, _ResolvedGroup
    from reader.users.repository import UserRepository

    repository = UserRepository(tmp_path / "users.db")
    source = TelegramSource(
        TelegramSettings(session_path_live=tmp_path / "live", session_path_sync=tmp_path / "sync",
                         api_id=1, api_hash="dummy", phone="+70000000000"),
        groups=[], user_repository=repository,
    )
    source._resolved[-1001] = _ResolvedGroup(entity=SimpleNamespace(username="VerhniyLars"), title="Верхний Ларс",
                                             identifier="VerhniyLars")
    try:
        await source.handle_new_message(_Event(chat_id=-1001, reply_to_msg_id=70))
        await source.handle_new_message(_Event(chat_id=-1009, reply_to_msg_id=None))  # группа не резолвлена
        stream = source.messages()
        try:
            first, second = await anext(stream), await anext(stream)
        finally:
            await stream.aclose()
        assert (first.chat_identifier, first.reply_to_msg_id, first.chat_title) == ("VerhniyLars", 70, "Верхний Ларс")
        assert (second.chat_identifier, second.reply_to_msg_id) == (None, None)
    finally:
        repository.close()
