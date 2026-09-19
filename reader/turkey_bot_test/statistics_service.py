"""TurkeyStatisticsService — trusted-manager "📊 Статистика" для Turkey-бота
(см. design report). Независимая реализация от
reader/public_bot/statistics_service.py (тот же принцип, что и везде в
reader/turkey_bot_test/ — standalone-бот, никаких кросс-импортов), но
СОЗНАТЕЛЬНО повторяет её архитектуру там, где она уже проверена: те же
business-day-boundary вычисления (см. _business_day_start_utc), тот же
приём "now/tz — явные параметры, не datetime.now() внутри" для
тестируемости.

ТОЛЬКО метрики, надёжно поддержанные текущей схемой Turkey-таблиц (см.
design report, аудит) — НИКАКИХ Георгия-специфичных метрик (active/
stopped subscriptions, monitoring tasks) — у Turkey нет мониторинга
вообще (см. задачу: "Do not copy the Georgian monitoring/subscription
architecture")."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from reader.turkey_bot_test.check_repository import TurkeyCheckRepository
from reader.turkey_bot_test.known_users_repository import TurkeyBotKnownUsersRepository


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


def _business_day_start_utc(now: datetime, tz: ZoneInfo, *, days_back: int) -> datetime:
    """Начало "делового дня" (days_back дней назад от now) в указанной
    business-timezone, выраженное обратно в UTC — тот же приём, что и
    reader/public_bot/statistics_service.py::_business_day_start_utc
    (скользящие ВКЛЮЧАЮЩИЕ окна: сегодня входит в 7д, 7д входит в 30д)."""
    business_date: date = now.astimezone(tz).date() - timedelta(days=days_back)
    local_midnight = datetime.combine(business_date, time.min, tzinfo=tz)
    return local_midnight.astimezone(timezone.utc)


class TurkeyStatisticsService:
    def __init__(
        self,
        known_users_repository: TurkeyBotKnownUsersRepository,
        check_repository: TurkeyCheckRepository,
    ):
        self._known_users = known_users_repository
        self._checks = check_repository

    def get_statistics(self, *, now: datetime, tz: ZoneInfo) -> TurkeyStatistics:
        """now — aware UTC, tz — та же business-timezone, что и у
        ConversationController (settings.fine_monitor.timezone, тот же
        общий конфиг, что и у Георгии — см. задачу: "reuse ... if a shared
        setting already exists", распространено здесь и на timezone, не
        только на trusted ID)."""
        today_start = _business_day_start_utc(now, tz, days_back=0)
        seven_days_start = _business_day_start_utc(now, tz, days_back=6)
        thirty_days_start = _business_day_start_utc(now, tz, days_back=29)

        return TurkeyStatistics(
            total_users=self._known_users.count_total(),
            new_users_today=self._known_users.count_first_seen_since(today_start),
            new_users_7d=self._known_users.count_first_seen_since(seven_days_start),
            new_users_30d=self._known_users.count_first_seen_since(thirty_days_start),
            total_checks=self._checks.count_total(),
            checks_today=self._checks.count_since(today_start),
            checks_7d=self._checks.count_since(seven_days_start),
            checks_30d=self._checks.count_since(thirty_days_start),
            checks_has_debt=self._checks.count_by_status("has_debt"),
            checks_no_debt=self._checks.count_by_status("no_debt"),
        )

    def list_known_users(self) -> list[tuple[int, str | None]]:
        """Прокси к TurkeyBotKnownUsersRepository.list_all() — здесь, а не
        напрямую в conversation.py, только чтобы вся "статистика" (счётчики
        + список) собиралась из одного места."""
        return self._known_users.list_all()
