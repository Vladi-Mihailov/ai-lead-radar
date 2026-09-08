"""BotStatisticsService — операторская "📊 Статистика" (см. design report):
краткая сводка использования @ProtocolGEbot поверх уже существующих
bot_known_users/fine_monitoring_subscriptions/detected_fines. Никакой новой
tracking-таблицы — все метрики посчитаны обычными SQL COUNT/WHERE-запросами
уже существующих репозиториев (см. задачу: "не загружать все rows в Python
ради подсчёта").

"Проверок сегодня" сюда намеренно НЕ включено: fine_monitoring_tasks.
last_checked_at — это ПОСЛЕДНИЙ момент проверки конкретной задачи
(перезаписывается на каждую проверку), а не лог отдельных проверок —
честно посчитать "сколько проверок произошло сегодня" по этому полю
нельзя (задача, проверенная 3 раза за день, даст ту же картину, что и
проверенная 1 раз) — надёжного source-of-truth для этой метрики в текущей
схеме нет (см. audit report).
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from reader.fines.detected_fine_repository import DetectedFineRepository
from reader.public_bot.known_users_repository import BotKnownUsersRepository
from reader.public_bot.subscription_repository import FineSubscriptionRepository


@dataclass(frozen=True)
class BotStatistics:
    total_users: int
    new_users_today: int
    new_users_7d: int
    new_users_30d: int
    active_subscriptions: int
    stopped_subscriptions: int
    # "🚨 Новых штрафов найдено сегодня" — единственная optional-метрика с
    # надёжным source-of-truth (detected_fines.first_detected_at, см.
    # модуль docstring) — всегда int, не Optional: либо считаем честно,
    # либо не добавляем метрику вовсе (см. задачу).
    new_fines_today: int


def _business_day_start_utc(now: datetime, tz: ZoneInfo, *, days_back: int) -> datetime:
    """Начало "делового дня" (days_back дней назад от now) в указанной
    business-timezone, выраженное обратно в UTC — тот же принцип, что и
    ConversationController._today()/ClientDeliveryService.run_once
    (now.astimezone(tz).date()), а не "случайный UTC" (см. задачу:
    "используй ту же timezone/business-date convention, которая уже
    применяется в проекте для Georgia")."""
    business_date: date = now.astimezone(tz).date() - timedelta(days=days_back)
    local_midnight = datetime.combine(business_date, time.min, tzinfo=tz)
    return local_midnight.astimezone(timezone.utc)


class BotStatisticsService:
    def __init__(
        self,
        known_users_repository: BotKnownUsersRepository,
        subscription_repository: FineSubscriptionRepository,
        detected_fine_repository: DetectedFineRepository,
    ):
        self._known_users = known_users_repository
        self._subscriptions = subscription_repository
        self._detected_fines = detected_fine_repository

    def get_statistics(self, *, now: datetime, tz: ZoneInfo) -> BotStatistics:
        """now — aware UTC (см. datetime.now(timezone.utc) везде в
        проекте), tz — та же business-timezone, что и у
        ConversationController (settings.fine_monitor.timezone). "За 7
        дней"/"за 30 дней" — включающие текущий business-день скользящие
        окна (today входит в 7d, 7d входит в 30d), а не отдельные
        непересекающиеся периоды."""
        today_start = _business_day_start_utc(now, tz, days_back=0)
        seven_days_start = _business_day_start_utc(now, tz, days_back=6)
        thirty_days_start = _business_day_start_utc(now, tz, days_back=29)

        return BotStatistics(
            total_users=self._known_users.count_total(),
            new_users_today=self._known_users.count_first_seen_since(today_start),
            new_users_7d=self._known_users.count_first_seen_since(seven_days_start),
            new_users_30d=self._known_users.count_first_seen_since(thirty_days_start),
            active_subscriptions=self._subscriptions.count_by_status("active"),
            stopped_subscriptions=self._subscriptions.count_by_status("stopped"),
            new_fines_today=self._detected_fines.count_first_detected_since(today_start),
        )
