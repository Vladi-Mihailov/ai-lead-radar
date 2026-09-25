"""TurkeyStatisticsService — trusted-manager "📊 Статистика" для Turkey-бота
(см. design report "Перестроить UX Turkey test bot", п.13). Независимая
реализация от reader/public_bot/statistics_service.py (никаких
кросс-импортов), СОЗНАТЕЛЬНО повторяет её архитектуру там, где она уже
проверена: те же business-day-boundary вычисления (см.
_business_day_start_utc), тот же приём "now/tz — явные параметры".

Источник данных для проверок — TurkeyCheckRunRepository (turkey_check_runs/
turkey_provider_results, unified-check, см. reader/turkey_bot/unified/
run_repository.py), НЕ старый TurkeyCheckRepository (turkey_fine_checks,
только GIB, старый one-shot flow, выведенный из употребления, см. design
report) — новая unified-архитектура покрывает все три провайдера
одинаково."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.models import TurkeyUserCar
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.unified.models import OverallStatus
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository

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
    manual_checks: int
    scheduled_checks: int
    active_monitoring_subscriptions: int
    provider_error_counts: dict[str, int]


@dataclass(frozen=True)
class TurkeyDebtRow:
    """Одна строка "🚨 Задолженность по последней проверке" (см. задачу
    "доработать 📊 Статистика Turkey bot") — ОДНА turkey_bot_user_cars
    строка (car_id/owner_telegram_user_id — конкретная запись, НЕ просто
    car_number, см. задачу п.8: "не склеивай разных владельцев только по
    номеру"), с уже готовым authoritative total_amount её последней
    ДОСТОВЕРНОЙ (не ERROR) проверки (см.
    TurkeyCheckRunRepository.get_latest_reliable_for_owner) — никакого
    пересчёта GİB+Avrasya+KGM здесь нет и не может быть."""

    car_id: int
    car_number: str
    owner_telegram_user_id: int
    total_amount: Decimal
    is_partial: bool
    checked_at: datetime


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
        отдельный, hardcoded в reader/turkey_bot/monitoring/
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
            manual_checks=self._runs.count_by_initiator("manual"),
            scheduled_checks=self._runs.count_by_initiator_prefix("monitoring_"),
            active_monitoring_subscriptions=self._subscriptions.count_active(),
            provider_error_counts=provider_error_counts,
        )

    def get_debt_rows(self, cars: list[TurkeyUserCar]) -> list[TurkeyDebtRow]:
        """"🚨 Задолженность по последней проверке" (см. задачу "доработать
        📊 Статистика Turkey bot") — ДЛЯ КАЖДОЙ переданной строки
        turkey_bot_user_cars читает её последний ДОСТОВЕРНЫЙ (не ERROR)
        unified-результат (см. TurkeyCheckRunRepository.
        get_latest_reliable_for_owner — owner-specific, тот же принцип,
        что и get_latest_for_owner у manager/trusted Search, см. задачу
        п.8) — НИКАКИХ provider/network requests, только SQL reads поверх
        уже сохранённых turkey_check_runs (см. задачу "КРИТИЧЕСКИ ВАЖНО:
        никаких live check"). cars передаётся вызывающим кодом (см.
        reader/turkey_bot/conversation.py::handle_statistics) — этот метод
        сознательно не тянет TurkeyUserCarsRepository как ещё одну
        constructor-зависимость (не менять существующую сигнатуру
        TurkeyStatisticsService.__init__, у которой уже много вызывающих
        мест).

        Пропускает машину, если: 1) ни одной ДОСТОВЕРНОЙ проверки ещё не
        было вовсе, 2) последняя достоверная проверка подтверждает 0 (нет
        задолженности) — см. задачу: "latest SUCCESS с 0 debt не
        включается". PARTIAL с total_amount > 0 включается, помеченным
        (см. is_partial) — задача явно требует не выдавать частичную
        сумму за гарантированно полную, но не скрывать её тоже.

        Сортировка — по сумме DESC, при равенстве — по свежести проверки
        DESC (см. задачу п.9) — никаких новых проверок ради сортировки,
        только уже вычисленные значения."""
        rows: list[TurkeyDebtRow] = []
        for car in cars:
            reliable = self._runs.get_latest_reliable_for_owner(
                plate=car.car_number, telegram_user_id=car.telegram_user_id,
            )
            if reliable is None:
                continue
            overall_status, total_amount, checked_at = reliable
            if total_amount <= 0:
                continue
            rows.append(
                TurkeyDebtRow(
                    car_id=car.id, car_number=car.car_number, owner_telegram_user_id=car.telegram_user_id,
                    total_amount=total_amount, is_partial=(overall_status == OverallStatus.PARTIAL),
                    checked_at=checked_at,
                )
            )
        rows.sort(key=lambda row: (-row.total_amount, -row.checked_at.timestamp()))
        return rows

    def get_known_username(self, telegram_user_id: int) -> str | None:
        """Прокси к TurkeyBotKnownUsersRepository.get_username() — ТОЛЬКО
        этот единичный lookup нужен manager/trusted-operator "🚗 Мои
        автомобили" (см. ConversationController._owner_username_display),
        чтобы резолвить username ВЛАДЕЛЬЦА каждой строки (по
        turkey_bot_user_cars.telegram_user_id), не вызывающего менеджера —
        переиспользует уже открытый here known_users_repository вместо
        нового constructor-параметра в ConversationController."""
        return self._known_users.get_username(telegram_user_id)

    def find_username(self, username: str) -> tuple[int, str] | None:
        """Прокси к TurkeyBotKnownUsersRepository.find_by_username() — тот
        же приём, что и get_known_username() выше, для manager/trusted
        Search (см. задачу "manager/trusted Search" — @username -> numeric
        telegram_user_id), без нового constructor-параметра в
        ConversationController."""
        return self._known_users.find_by_username(username)

    def find_name(self, query: str) -> list[tuple[int, str | None, str | None, str | None]]:
        """Прокси к TurkeyBotKnownUsersRepository.find_by_name() — см.
        задачу "add name search to trusted bot search" (имя -> numeric
        telegram_user_id, может быть НЕСКОЛЬКО совпадений — имена не
        уникальны)."""
        return self._known_users.find_by_name(query)

    def get_known_profile(self, telegram_user_id: int) -> tuple[str | None, str | None, str | None] | None:
        """Прокси к TurkeyBotKnownUsersRepository.get_profile() — см.
        задачу п.7: Search result должен показывать имя ВМЕСТЕ с username,
        не только username (см. get_known_username() выше)."""
        return self._known_users.get_profile(telegram_user_id)
