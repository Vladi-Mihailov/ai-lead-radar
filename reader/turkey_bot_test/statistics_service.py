"""TurkeyStatisticsService — trusted-manager "📊 Статистика" для Turkey-бота
(см. design report "Перестроить UX Turkey test bot", п.13). Независимая
реализация от reader/public_bot/statistics_service.py (никаких
кросс-импортов), СОЗНАТЕЛЬНО повторяет её архитектуру там, где она уже
проверена: те же business-day-boundary вычисления (см.
_business_day_start_utc), тот же приём "now/tz — явные параметры".

Источник данных для проверок — TurkeyCheckRunRepository (turkey_check_runs/
turkey_provider_results, unified-check, см. reader/turkey_bot_test/unified/
run_repository.py), НЕ старый TurkeyCheckRepository (turkey_fine_checks,
только GIB, старый one-shot flow, выведенный из употребления, см. design
report) — новая unified-архитектура покрывает все три провайдера
одинаково."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from reader.turkey_bot_test.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot_test.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot_test.unified.run_repository import TurkeyCheckRunRepository

_PROVIDERS = ("gib", "avrasya", "kgm")


@dataclass(frozen=True)
class TurkeyStatistics:
    total_users: int
    new_users_today: int
    new_users_7d: int
    new_users_30d: int
    total_checks: int
    checks_today: int
    checks_7d: int
    checks_30d: int
    checks_has_debt: int
    checks_no_debt: int
    checks_partial: int
    checks_error: int
    manual_checks: int
    scheduled_checks: int
    active_monitoring_subscriptions: int
    provider_error_counts: dict[str, int]


def _business_day_start_utc(now: datetime, tz: ZoneInfo, *, days_back: int) -> datetime:
    """Начало "делового дня" (days_back дней назад от now) в указанной
    business-timezone, выраженное обратно в UTC — тот же приём, что и
    reader/public_bot/statistics_service.py::_business_day_start_utc."""
    business_date: date = now.astimezone(tz).date() - timedelta(days=days_back)
    local_midnight = datetime.combine(business_date, time.min, tzinfo=tz)
    return local_midnight.astimezone(timezone.utc)


class TurkeyStatisticsService:
    def __init__(
        self,
        known_users_repository: TurkeyBotKnownUsersRepository,
        run_repository: TurkeyCheckRunRepository,
        subscription_repository: TurkeyMonitoringSubscriptionRepository,
    ):
        self._known_users = known_users_repository
        self._runs = run_repository
        self._subscriptions = subscription_repository

    def get_statistics(self, *, now: datetime, tz: ZoneInfo) -> TurkeyStatistics:
        """now — aware UTC, tz — business-timezone для "сегодня/7д/30д"
        (settings.fine_monitor.timezone, НЕ Europe/Istanbul — тот
        отдельный, hardcoded в reader/turkey_bot_test/monitoring/
        scheduler_job.py, используется только для расписания проверок,
        не для business-day статистики, см. design report)."""
        today_start = _business_day_start_utc(now, tz, days_back=0)
        seven_days_start = _business_day_start_utc(now, tz, days_back=6)
        thirty_days_start = _business_day_start_utc(now, tz, days_back=29)

        provider_error_counts = {
            provider: self._runs.count_provider_status(provider, "error") for provider in _PROVIDERS
        }

        return TurkeyStatistics(
            total_users=self._known_users.count_total(),
            new_users_today=self._known_users.count_first_seen_since(today_start),
            new_users_7d=self._known_users.count_first_seen_since(seven_days_start),
            new_users_30d=self._known_users.count_first_seen_since(thirty_days_start),
            total_checks=self._runs.count_total(),
            checks_today=self._runs.count_since(today_start),
            checks_7d=self._runs.count_since(seven_days_start),
            checks_30d=self._runs.count_since(thirty_days_start),
            checks_has_debt=self._runs.count_by_overall_status("has_debt"),
            checks_no_debt=self._runs.count_by_overall_status("no_debt"),
            checks_partial=self._runs.count_by_overall_status("partial"),
            checks_error=self._runs.count_by_overall_status("error"),
            manual_checks=self._runs.count_by_initiator("manual"),
            scheduled_checks=self._runs.count_by_initiator_prefix("monitoring_"),
            active_monitoring_subscriptions=self._subscriptions.count_active(),
            provider_error_counts=provider_error_counts,
        )

    def list_known_users(self) -> list[tuple[int, str | None]]:
        """Прокси к TurkeyBotKnownUsersRepository.list_all()."""
        return self._known_users.list_all()
