"""Изоляция новых unified/monitoring таблиц и модулей (см. design report
"Перестроить UX Turkey test bot" п.16: "КРИТИЧНО"). Новые таблицы живут в
ОТДЕЛЬНОМ test DB файле и не создаются/не пишутся в отдельный
"production-like" файл; новые модули не импортируют reader.turkey_bot/
reader.public_bot/reader.fines (reader.jobs — единственное явно
разрешённое исключение, см. design report решение п.1)."""

import ast
from pathlib import Path

from reader.turkey_bot_test.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot_test.unified.run_repository import TurkeyCheckRunRepository

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FORBIDDEN_PREFIXES = ("reader.turkey_bot.", "reader.public_bot.", "reader.fines.")
_FORBIDDEN_EXACT = ("reader.turkey_bot", "reader.public_bot", "reader.fines")


def _new_module_files() -> list[Path]:
    unified_dir = PROJECT_ROOT / "reader" / "turkey_bot_test" / "unified"
    monitoring_dir = PROJECT_ROOT / "reader" / "turkey_bot_test" / "monitoring"
    return sorted(unified_dir.glob("*.py")) + sorted(monitoring_dir.glob("*.py"))


def _imported_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_new_unified_and_monitoring_modules_do_not_import_shared_production_packages():
    """reader.jobs.* — единственное разрешённое исключение (см. design
    report решение п.1 "Вариант A": только импорт, reader/jobs/* не
    меняется)."""
    offenders = []
    for py_file in _new_module_files():
        for module in _imported_modules(py_file):
            if module in _FORBIDDEN_EXACT or module.startswith(_FORBIDDEN_PREFIXES):
                offenders.append((py_file.name, module))
    assert offenders == []


def test_new_modules_are_confined_to_turkey_bot_test_package():
    for py_file in _new_module_files():
        assert "reader/turkey_bot_test/" in py_file.as_posix()


def test_new_tables_live_in_isolated_db_file_not_a_shared_one(tmp_path):
    """Запись через новые репозитории на СВОЙ файл не создаёт/не трогает
    ОТДЕЛЬНЫЙ файл, имитирующий production (тот же принцип, что и
    test_turkey_bot_test_clone.py::
    test_test_clone_repositories_do_not_touch_production_repository_state,
    применённый к НОВЫМ unified/monitoring таблицам)."""
    prod_like_db = tmp_path / "prod_like.db"
    test_like_db = tmp_path / "test_like.db"

    prod_runs = TurkeyCheckRunRepository(prod_like_db)
    test_runs = TurkeyCheckRunRepository(test_like_db)
    prod_subs = TurkeyMonitoringSubscriptionRepository(prod_like_db)
    test_subs = TurkeyMonitoringSubscriptionRepository(test_like_db)

    test_subs.enable(telegram_user_id=1, telegram_chat_id=1, plate="A123AA123")

    assert test_subs.count_active() == 1
    assert prod_subs.count_active() == 0
    assert test_runs.count_total() == 0
    assert prod_runs.count_total() == 0

    prod_runs.close()
    test_runs.close()
    prod_subs.close()
    test_subs.close()


def test_new_tables_are_additive_do_not_touch_old_turkey_check_tables(tmp_path):
    """Новые таблицы (turkey_check_runs/turkey_provider_results/
    turkey_monitoring_subscriptions) сосуществуют в ОДНОМ файле со
    старыми (turkey_fine_checks/turkey_toll_checks/turkey_bot_user_cars)
    без конфликтов схемы — additive-миграция (см. design report п.6)."""
    from reader.turkey_bot_test.check_repository import TurkeyCheckRepository
    from reader.turkey_bot_test.user_cars_repository import TurkeyUserCarsRepository

    db_path = tmp_path / "shared.db"
    old_checks = TurkeyCheckRepository(db_path)
    old_checks.record_result(
        telegram_user_id=1, telegram_chat_id=1, plate="A123AA123",
        captcha_attempts=1, status="no_debt", gib_message_text=None, raw_response=None,
    )
    garage = TurkeyUserCarsRepository(db_path)
    garage.add_car(telegram_user_id=1, car_number="A123AA123")
    runs = TurkeyCheckRunRepository(db_path)
    subs = TurkeyMonitoringSubscriptionRepository(db_path)
    subs.enable(telegram_user_id=1, telegram_chat_id=1, plate="A123AA123")

    assert old_checks.count_total() == 1  # старая таблица не тронута
    assert len(garage.list_cars(1)) == 1
    assert runs.count_total() == 0
    assert subs.count_active() == 1

    old_checks.close()
    garage.close()
    runs.close()
    subs.close()
