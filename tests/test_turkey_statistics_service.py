"""
Тесты reader/turkey_bot/statistics_service.py::TurkeyStatisticsService —
ТОЛЬКО метрики, надёжно поддержанные текущей схемой Turkey-таблиц (см.
design report, аудит) — никаких Георгия-специфичных метрик.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.turkey_bot.check_repository import TurkeyCheckRepository  # noqa: E402
from reader.turkey_bot.known_users_repository import (
    TurkeyBotKnownUsersRepository,  # noqa: E402
)
from reader.turkey_bot.statistics_service import TurkeyStatisticsService  # noqa: E402

_TZ = ZoneInfo("UTC")
_NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def _service():
    known_users = TurkeyBotKnownUsersRepository(":memory:")
    checks = TurkeyCheckRepository(":memory:")
    return TurkeyStatisticsService(known_users, checks), known_users, checks


def _seed_user(known_users, *, telegram_user_id: int, days_ago: int, username: str | None = None) -> None:
    known_users.record_seen(
        telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, telegram_username=username,
    )
    seen_at = (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    known_users._conn.execute(
        "UPDATE turkey_bot_known_users SET first_seen_at = ? WHERE telegram_user_id = ?",
        (seen_at, telegram_user_id),
    )
    known_users._conn.commit()


def _seed_check(checks, *, telegram_user_id: int, status: str, days_ago: int) -> None:
    checks.record_result(
        telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id, plate="34ABC123",
        captcha_attempts=1, status=status, gib_message_text=None, raw_response=None,
    )
    requested_at = (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    checks._conn.execute(
        "UPDATE turkey_fine_checks SET requested_at = ? "
        "WHERE id = (SELECT MAX(id) FROM turkey_fine_checks)",
        (requested_at,),
    )
    checks._conn.commit()


def test_total_users_counts_everyone():
    service, known_users, _checks = _service()
    _seed_user(known_users, telegram_user_id=1, days_ago=0)
    _seed_user(known_users, telegram_user_id=2, days_ago=100)

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.total_users == 2


def test_new_users_today_7d_30d_are_rolling_inclusive_windows():
    service, known_users, _checks = _service()
    _seed_user(known_users, telegram_user_id=1, days_ago=0)   # today
    _seed_user(known_users, telegram_user_id=2, days_ago=5)   # within 7d
    _seed_user(known_users, telegram_user_id=3, days_ago=20)  # within 30d
    _seed_user(known_users, telegram_user_id=4, days_ago=60)  # outside all windows

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.new_users_today == 1
    assert stats.new_users_7d == 2
    assert stats.new_users_30d == 3
    assert stats.total_users == 4


def test_total_checks_counts_all_recorded_rows():
    service, _known_users, checks = _service()
    _seed_check(checks, telegram_user_id=1, status="no_debt", days_ago=0)
    _seed_check(checks, telegram_user_id=1, status="has_debt", days_ago=0)
    _seed_check(checks, telegram_user_id=1, status="unexpected", days_ago=0)

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.total_checks == 3


def test_checks_today_7d_30d_are_rolling_inclusive_windows():
    service, _known_users, checks = _service()
    _seed_check(checks, telegram_user_id=1, status="no_debt", days_ago=0)
    _seed_check(checks, telegram_user_id=1, status="no_debt", days_ago=5)
    _seed_check(checks, telegram_user_id=1, status="no_debt", days_ago=20)
    _seed_check(checks, telegram_user_id=1, status="no_debt", days_ago=60)

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.checks_today == 1
    assert stats.checks_7d == 2
    assert stats.checks_30d == 3
    assert stats.total_checks == 4


def test_has_debt_and_no_debt_breakdown():
    service, _known_users, checks = _service()
    _seed_check(checks, telegram_user_id=1, status="has_debt", days_ago=0)
    _seed_check(checks, telegram_user_id=1, status="has_debt", days_ago=0)
    _seed_check(checks, telegram_user_id=1, status="no_debt", days_ago=0)
    _seed_check(checks, telegram_user_id=1, status="unexpected", days_ago=0)

    stats = service.get_statistics(now=_NOW, tz=_TZ)

    assert stats.checks_has_debt == 2
    assert stats.checks_no_debt == 1


def test_list_known_users_delegates_to_repository_ordering():
    service, known_users, _checks = _service()
    _seed_user(known_users, telegram_user_id=2, days_ago=1, username="bob")
    _seed_user(known_users, telegram_user_id=1, days_ago=5, username="alice")

    users = service.list_known_users()

    assert users == [(1, "alice"), (2, "bob")]
