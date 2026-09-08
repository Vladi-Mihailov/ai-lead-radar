"""
Тесты BotStatisticsService (reader/public_bot/statistics_service.py) —
"📊 Статистика" (trusted-operator-only, см. design report). Repository —
настоящие (SQLite/tmp_path), никакого Telegram/handler здесь нет.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from reader.fines.detected_fine_repository import DetectedFineRepository  # noqa: E402
from reader.fines.task_repository import FineMonitoringTaskRepository  # noqa: E402
from reader.public_bot.known_users_repository import BotKnownUsersRepository  # noqa: E402
from reader.public_bot.statistics_service import BotStatisticsService  # noqa: E402
from reader.public_bot.subscription_repository import FineSubscriptionRepository  # noqa: E402

_CHAT_ID = -100999
_USER_ID = 111
_TBILISI = ZoneInfo("Asia/Tbilisi")


class _Fixture:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "users.db"
        self.task_repository = FineMonitoringTaskRepository(self.db_path)
        self.detected_fine_repository = DetectedFineRepository(self.db_path)
        self.subscription_repository = FineSubscriptionRepository(self.db_path)
        self.known_users_repository = BotKnownUsersRepository(self.db_path)
        self.service = BotStatisticsService(
            self.known_users_repository, self.subscription_repository, self.detected_fine_repository,
        )

    def make_task(self, car_number="B957MA09") -> int:
        return self.task_repository.create(
            car_number=car_number, label=None,
            start_date=date(2026, 8, 1), end_date=date(2026, 12, 31),
            telegram_chat_id=_CHAT_ID, created_by_user_id=_USER_ID,
        ).id

    def close(self):
        self.task_repository.close()
        self.detected_fine_repository.close()
        self.subscription_repository.close()
        self.known_users_repository.close()


def test_empty_database_yields_all_zeros(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        stats = fx.service.get_statistics(now=datetime.now(timezone.utc), tz=_TBILISI)

        assert stats.total_users == 0
        assert stats.new_users_today == 0
        assert stats.new_users_7d == 0
        assert stats.new_users_30d == 0
        assert stats.active_subscriptions == 0
        assert stats.stopped_subscriptions == 0
        assert stats.new_fines_today == 0
    finally:
        fx.close()


def test_total_users_counts_all_known_users(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        fx.known_users_repository.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)
        fx.known_users_repository.record_seen(telegram_user_id=2, telegram_chat_id=2, telegram_username=None)

        stats = fx.service.get_statistics(now=datetime.now(timezone.utc), tz=_TBILISI)

        assert stats.total_users == 2
    finally:
        fx.close()


def test_active_and_stopped_subscriptions_counted_by_status(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task()
        alice = fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=1, telegram_chat_id=1, telegram_username="alice",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        fx.subscription_repository.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            telegram_user_id=2, telegram_chat_id=2, telegram_username="bob",
            start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
        )
        fx.subscription_repository.stop_by_owner_or_creator(alice.id, telegram_user_id=1)

        stats = fx.service.get_statistics(now=datetime.now(timezone.utc), tz=_TBILISI)

        assert stats.active_subscriptions == 1
        assert stats.stopped_subscriptions == 1
    finally:
        fx.close()


# ---- new users today / 7d / 30d — business-date (Asia/Tbilisi) boundary ----


def test_new_users_today_counts_only_users_from_current_business_day(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)  # 14:00 по Тбилиси

        # "Вчера" по Тбилиси (даже если по UTC ещё тот же день) — НЕ "сегодня".
        fx.known_users_repository._conn.execute(
            "INSERT INTO bot_known_users "
            "(telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at) "
            "VALUES (1, 1, 'yesterday_user', '2026-09-07 12:00:00', '2026-09-07 12:00:00')"
        )
        # Сегодня по Тбилиси (05:30 UTC = 09:30 по Тбилиси, уже новые сутки).
        fx.known_users_repository._conn.execute(
            "INSERT INTO bot_known_users "
            "(telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at) "
            "VALUES (2, 2, 'today_user', '2026-09-08 05:30:00', '2026-09-08 05:30:00')"
        )
        fx.known_users_repository._conn.commit()

        stats = fx.service.get_statistics(now=now, tz=_TBILISI)

        assert stats.new_users_today == 1
        assert stats.total_users == 2
    finally:
        fx.close()


def test_business_date_boundary_differs_from_utc_midnight(tmp_path):
    """Явное требование задачи: "не считать сегодня по случайному UTC" —
    00:30 по Тбилиси (= 20:30 UTC предыдущего дня) должно попадать в
    "сегодня" по Тбилиси, а не в "вчера" по UTC."""
    fx = _Fixture(tmp_path)
    try:
        # now = 2026-09-08 00:30 по Тбилиси (Asia/Tbilisi, UTC+4) = 2026-09-07 20:30 UTC.
        now = datetime(2026, 9, 7, 20, 30, tzinfo=timezone.utc)

        # Пользователь появился в 2026-09-08 00:15 по Тбилиси = 2026-09-07 20:15 UTC —
        # ДО now, но в тот же business-день (Тбилиси), что и now.
        fx.known_users_repository._conn.execute(
            "INSERT INTO bot_known_users "
            "(telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at) "
            "VALUES (1, 1, 'tbilisi_today', '2026-09-07 20:15:00', '2026-09-07 20:15:00')"
        )
        fx.known_users_repository._conn.commit()

        stats = fx.service.get_statistics(now=now, tz=_TBILISI)

        # По UTC-полуночи этот пользователь был бы "вчера" (создан 2026-09-07
        # по UTC); по Тбилиси-business-дню он "сегодня" (2026-09-08 00:15).
        assert stats.new_users_today == 1
    finally:
        fx.close()


def test_new_users_7d_includes_today_and_previous_six_days(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)

        # Ровно на границе 7-дневного окна (today - 6 дней, 00:00 по Тбилиси).
        fx.known_users_repository._conn.execute(
            "INSERT INTO bot_known_users "
            "(telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at) "
            "VALUES (1, 1, 'seven_days_ago', '2026-09-01 20:00:00', '2026-09-01 20:00:00')"
        )
        # Строго ЗА пределами окна — на день раньше границы.
        fx.known_users_repository._conn.execute(
            "INSERT INTO bot_known_users "
            "(telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at) "
            "VALUES (2, 2, 'eight_days_ago', '2026-08-31 10:00:00', '2026-08-31 10:00:00')"
        )
        fx.known_users_repository._conn.commit()

        stats = fx.service.get_statistics(now=now, tz=_TBILISI)

        assert stats.new_users_7d == 1
        assert stats.total_users == 2
    finally:
        fx.close()


def test_new_users_30d_includes_today_and_previous_twenty_nine_days(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)

        # today (2026-09-08 по Тбилиси) - 29 дней = 2026-08-10 00:00 по Тбилиси.
        fx.known_users_repository._conn.execute(
            "INSERT INTO bot_known_users "
            "(telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at) "
            "VALUES (1, 1, 'thirty_days_ago', '2026-08-09 21:00:00', '2026-08-09 21:00:00')"
        )
        fx.known_users_repository._conn.execute(
            "INSERT INTO bot_known_users "
            "(telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at) "
            "VALUES (2, 2, 'thirty_one_days_ago', '2026-08-08 10:00:00', '2026-08-08 10:00:00')"
        )
        fx.known_users_repository._conn.commit()

        stats = fx.service.get_statistics(now=now, tz=_TBILISI)

        assert stats.new_users_30d == 1
    finally:
        fx.close()


def test_new_users_counts_are_cumulative_supersets(tmp_path):
    """today <= 7d <= 30d по построению (скользящие окна, а не отдельные
    непересекающиеся периоды)."""
    fx = _Fixture(tmp_path)
    try:
        now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
        fx.known_users_repository.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username=None)

        stats = fx.service.get_statistics(now=now, tz=_TBILISI)

        assert stats.new_users_today <= stats.new_users_7d <= stats.new_users_30d
    finally:
        fx.close()


def test_repeated_activity_does_not_count_old_user_as_new(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        fx.known_users_repository._conn.execute(
            "INSERT INTO bot_known_users "
            "(telegram_user_id, telegram_chat_id, telegram_username, first_seen_at, last_seen_at) "
            "VALUES (1, 1, 'old_user', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        fx.known_users_repository._conn.commit()
        # Пользователь снова написал боту "сегодня".
        fx.known_users_repository.record_seen(telegram_user_id=1, telegram_chat_id=1, telegram_username="old_user")

        stats = fx.service.get_statistics(now=datetime.now(timezone.utc), tz=_TBILISI)

        assert stats.new_users_today == 0
        assert stats.total_users == 1
    finally:
        fx.close()


# ---- new_fines_today — optional metric, source-of-truth = first_detected_at ----


def test_new_fines_today_counts_only_genuinely_new_fines(tmp_path):
    fx = _Fixture(tmp_path)
    try:
        task_id = fx.make_task()
        fine = fx.detected_fine_repository.create(
            monitoring_task_id=task_id, car_number="B957MA09",
            external_fine_id="AB1", fingerprint="fp-1",
            penalty_date=date(2026, 8, 6), due_date=date(2026, 8, 20),
            delivered_status=None, raw_data="{}",
        )
        fx.detected_fine_repository.mark_seen(fine.id)  # повторное обнаружение — не новое

        stats = fx.service.get_statistics(now=datetime.now(timezone.utc), tz=_TBILISI)

        assert stats.new_fines_today == 1
    finally:
        fx.close()
