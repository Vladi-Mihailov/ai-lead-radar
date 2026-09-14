"""
Тесты reader/turkey_bot/check_repository.py — append-only журнал
завершённых одноразовых проверок (см. design report Stage 3), НЕ
используется для восстановления GIB-сессии.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.check_repository import TurkeyCheckRepository  # noqa: E402


def _repo() -> TurkeyCheckRepository:
    return TurkeyCheckRepository(":memory:")


def test_record_result_inserts_one_row():
    repo = _repo()

    repo.record_result(
        telegram_user_id=1, telegram_chat_id=1, plate="34ABC123",
        captcha_attempts=1, status="no_debt", gib_message_text="borç bulunamadı",
        raw_response='{"kind": "no_debt"}',
    )

    assert repo.count_total() == 1
    assert repo.count_by_status("no_debt") == 1
    assert repo.count_by_status("has_debt") == 0


def test_count_by_status_distinguishes_statuses():
    repo = _repo()
    repo.record_result(
        telegram_user_id=1, telegram_chat_id=1, plate="34ABC123", captcha_attempts=1,
        status="no_debt", gib_message_text=None, raw_response=None,
    )
    repo.record_result(
        telegram_user_id=2, telegram_chat_id=2, plate="06XYZ999", captcha_attempts=3,
        status="rejected_exhausted", gib_message_text=None, raw_response=None,
    )
    repo.record_result(
        telegram_user_id=3, telegram_chat_id=3, plate="06XYZ999", captcha_attempts=1,
        status="unexpected", gib_message_text="???", raw_response="{}",
    )

    assert repo.count_total() == 3
    assert repo.count_by_status("no_debt") == 1
    assert repo.count_by_status("rejected_exhausted") == 1
    assert repo.count_by_status("unexpected") == 1


def test_stores_null_message_and_response_when_absent():
    repo = _repo()
    repo.record_result(
        telegram_user_id=1, telegram_chat_id=1, plate="34ABC123", captcha_attempts=1,
        status="error", gib_message_text=None, raw_response=None,
    )

    row = repo._conn.execute(
        "SELECT gib_message_text, raw_response FROM turkey_fine_checks WHERE id = 1"
    ).fetchone()
    assert row == (None, None)
