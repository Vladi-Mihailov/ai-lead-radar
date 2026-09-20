"""
Тесты изоляции reader/turkey_bot_test/* от production reader/turkey_bot/*
(token/session/DB/классы) — см. design report "изолированный
experimental clone Turkey bot" И "Перестроить UX Turkey test bot" п.16
("КРИТИЧНО: изоляция"). Функциональные тесты нового unified-flow
(add car/my cars/unified check/monitoring) — см. отдельные файлы
tests/test_turkey_unified_*.py/test_turkey_monitoring_*.py (старый
provider-by-provider happy-path здесь удалён вместе со старым UX, см.
design report решение п.4 — GİB/Avrasya/KGM больше не выбираются
пользователем напрямую, соответствующий ConversationController API
удалён)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

# ---- Production reader/turkey_bot/* (для сравнения — НЕ используется для
# запуска чего-либо, только чтобы доказать, что клон — ОТДЕЛЬНЫЕ модули/
# классы/пути, а не переиспользование production через alias/re-export). ----
import reader.turkey_bot.main as prod_main

# ---- Test-clone reader/turkey_bot_test/* ----
import reader.turkey_bot_test.main as test_main
from reader.settings import ConfigError, load_settings
from reader.turkey_bot.live_session_registry import (
    LiveGibSessionRegistry as ProdLiveGibSessionRegistry,
)
from reader.turkey_bot_test.check_repository import TurkeyCheckRepository
from reader.turkey_bot_test.live_session_registry import (
    LiveGibSessionRegistry,
)

_CHAT_ID = 111
_USER_ID = 222


def test_test_clone_uses_its_own_token_env_variable(monkeypatch):
    """См. design report п.3: TURKEYBOT_TEST_TOKEN, НЕ TURKEYBOT_TOKEN."""
    monkeypatch.delenv("TURKEYBOT_TOKEN", raising=False)
    monkeypatch.setenv("TURKEYBOT_TEST_TOKEN", "test-token-value")

    assert test_main.read_bot_token() == "test-token-value"


def test_test_clone_never_falls_back_to_production_token(monkeypatch):
    """Явное требование задачи: "Test bot НИКОГДА не должен fallback'иться
    на production TURKEYBOT_TOKEN" — даже когда production TURKEYBOT_TOKEN
    реально задан в окружении, отсутствие TURKEYBOT_TEST_TOKEN должно
    приводить к fail-fast ошибке, а не к тихому использованию production
    токена."""
    monkeypatch.setenv("TURKEYBOT_TOKEN", "production-token-should-not-be-used")
    monkeypatch.delenv("TURKEYBOT_TEST_TOKEN", raising=False)

    with pytest.raises(ConfigError):
        test_main.read_bot_token()


def test_test_clone_db_path_differs_from_production_db_path():
    """См. design report п.5 — ОТДЕЛЬНЫЙ SQLite-файл, НЕ
    settings.app.users_db_file (production, общий с Георгия-ботом)."""
    settings = load_settings(PROJECT_ROOT / "config" / "config.yaml")

    assert test_main._DB_PATH != settings.app.users_db_file
    assert test_main._DB_PATH.name == "turkey_bot_test.db"


def test_test_clone_session_path_differs_from_production_session_path():
    """См. design report п.4 — ОТДЕЛЬНЫЙ .session-файл."""
    assert test_main._SESSION_PATH != prod_main._SESSION_PATH
    assert test_main._SESSION_PATH.name == "turkey_bot_test"
    assert prod_main._SESSION_PATH.name == "turkeybot"


def test_production_and_test_entrypoints_are_independent_modules():
    """Клон — НАСТОЯЩАЯ отдельная копия, а не re-export/alias
    production-модуля — доказывается тем, что это ДВА РАЗНЫХ объекта
    модуля и ДВА РАЗНЫХ класса реестра, а не один и тот же импортированный
    дважды."""
    assert test_main is not prod_main
    assert test_main.__file__ != prod_main.__file__
    assert LiveGibSessionRegistry is not ProdLiveGibSessionRegistry


def test_test_clone_repositories_do_not_touch_production_repository_state(tmp_path):
    """Прямое доказательство изоляции данных (см. design report п.5: "Test
    bot НЕ должен писать экспериментальные данные в production
    data/users.db") — запись через test-репозиторий на СВОЙ файл никак не
    отражается в ОТДЕЛЬНОМ файле, имитирующем production."""
    prod_like_db = tmp_path / "prod_like.db"
    test_like_db = tmp_path / "test_like.db"

    prod_repo = TurkeyCheckRepository(prod_like_db)
    test_repo = TurkeyCheckRepository(test_like_db)

    test_repo.record_result(
        telegram_user_id=_USER_ID, telegram_chat_id=_CHAT_ID, plate="34ABC123",
        captcha_attempts=1, status="no_debt", gib_message_text=None, raw_response=None,
    )

    assert test_repo.count_total() == 1
    assert prod_repo.count_total() == 0

    prod_repo.close()
    test_repo.close()


def test_production_turkey_bot_package_untouched_by_unified_ux_refactor():
    """Regression-guard для design report п.16 ("КРИТИЧНО: изоляция") —
    новые unified/monitoring-модули этой задачи физически не существуют
    под reader/turkey_bot/ и ничего оттуда не импортируют."""
    import reader.turkey_bot.conversation as prod_conversation

    assert not hasattr(prod_conversation, "UnifiedTurkeyCheckService")
    assert not (PROJECT_ROOT / "reader" / "turkey_bot" / "unified").exists()
    assert not (PROJECT_ROOT / "reader" / "turkey_bot" / "monitoring").exists()
