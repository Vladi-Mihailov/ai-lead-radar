"""
Тесты reader/turkey_bot/toll_check_repository.py — append-only журнал
завершённых одноразовых Avrasya-проверок (см. design report Stage 2B) —
структурная копия test_turkey_check_repository.py (GIB), ОТДЕЛЬНАЯ
таблица turkey_toll_checks, никогда не трогает turkey_fine_checks.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.toll_check_repository import (
    TurkeyTollCheckRepository,  # noqa: E402
)


def _repo() -> TurkeyTollCheckRepository:
    return TurkeyTollCheckRepository(":memory:")


def test_record_result_inserts_one_row_with_default_provider():
    repo = _repo()

    repo.record_result(
        telegram_user_id=1, telegram_chat_id=1, plate="A123AA123",
        captcha_attempts=1, status="no_debt", message_text=None,
        raw_response='{"kind": "no_debt"}',
    )

    assert repo.count_total() == 1
    assert repo.count_by_status("no_debt") == 1
    assert repo.count_by_status("has_debt") == 0

    row = repo._conn.execute("SELECT provider FROM turkey_toll_checks WHERE id = 1").fetchone()
    assert row == ("avrasya",)


def test_count_by_status_distinguishes_statuses():
    repo = _repo()
    repo.record_result(
        telegram_user_id=1, telegram_chat_id=1, plate="A123AA123", captcha_attempts=1,
        status="no_debt", message_text=None, raw_response=None,
    )
    repo.record_result(
        telegram_user_id=2, telegram_chat_id=2, plate="A777AA777", captcha_attempts=3,
        status="error", message_text=None, raw_response=None,
    )
    repo.record_result(
        telegram_user_id=3, telegram_chat_id=3, plate="A777AA777", captcha_attempts=1,
        status="unexpected", message_text="???", raw_response="{}",
    )

    assert repo.count_total() == 3
    assert repo.count_by_status("no_debt") == 1
    assert repo.count_by_status("error") == 1
    assert repo.count_by_status("unexpected") == 1


def test_stores_null_message_and_response_when_absent():
    repo = _repo()
    repo.record_result(
        telegram_user_id=1, telegram_chat_id=1, plate="A123AA123", captcha_attempts=1,
        status="error", message_text=None, raw_response=None,
    )

    row = repo._conn.execute(
        "SELECT message_text, raw_response FROM turkey_toll_checks WHERE id = 1"
    ).fetchone()
    assert row == (None, None)


def test_does_not_touch_gib_fine_checks_table():
    """См. design report Stage 1: "additive... do not modify existing
    GİB historical data" — таблица turkey_fine_checks не должна даже
    существовать в этой БД, если её никто не создавал."""
    repo = _repo()
    repo.record_result(
        telegram_user_id=1, telegram_chat_id=1, plate="A123AA123", captcha_attempts=1,
        status="no_debt", message_text=None, raw_response=None,
    )

    tables = {
        row[0]
        for row in repo._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "turkey_fine_checks" not in tables
    assert "turkey_toll_checks" in tables
