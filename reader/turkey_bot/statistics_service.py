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
from enum import StrEnum
from zoneinfo import ZoneInfo

from reader.turkey_bot.known_users_repository import TurkeyBotKnownUsersRepository
from reader.turkey_bot.models import TurkeyUserCar
from reader.turkey_bot.monitoring.subscription_repository import (
    TurkeyMonitoringSubscriptionRepository,
)
from reader.turkey_bot.unified.models import ProviderStatus
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository

_PROVIDERS = ("gib", "avrasya", "kgm")

# Провайдеры, из которых складывается задолженность автомобиля в "🚨
# Задолженность по последней проверке": GİB + KGM. Avrasya НЕ входит — её
# долг уже учтён внутри KGM ("Платные дороги, включая тунели"), см.
# reader/turkey_bot/unified/models.py::total_amount_for/
# _EXCLUDED_FROM_OVERALL_TOTAL — прибавлять его ещё раз задвоило бы сумму.
_DEBT_PROVIDERS = ("gib", "kgm")


class TurkeyProviderFreshness(StrEnum):
    """Насколько свеж известный результат ОДНОГО провайдера по номеру (см.
    задачу "Turkey debt list: последний успешный результат отдельно по
    каждому провайдеру и госномеру"):
      - CURRENT — последняя попытка этого провайдера успешна, сумма
        подтверждена последней проверкой;
      - STALE — последняя попытка завершилась ошибкой (unexpected/
        transport_error/captcha_*/…), действует последний успешный
        результат — это ПОСЛЕДНЯЯ ИЗВЕСТНАЯ сумма, а не подтверждённая
        сегодня;
      - NO_DATA — провайдер ни разу не ответил успешно (или номер вообще
        не проверялся) — достоверных данных нет.
    Ошибка провайдера никогда не превращается в "долга нет"."""

    CURRENT = "current"
    STALE = "stale"
    NO_DATA = "no_data"


@dataclass(frozen=True)
class TurkeyProviderDebtState:
    """Состояние ОДНОГО провайдера (GİB или KGM) по номеру: amount — долг
    по последнему УСПЕШНОМУ результату (0 — успешная проверка без долга
    или NO_DATA); last_success_at/last_attempt_at — finished_at
    соответствующих runs (None, если их не было); last_attempt_error_type
    — error_type последней попытки, если она — ошибка."""

    provider: str
    freshness: TurkeyProviderFreshness
    amount: Decimal
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    last_attempt_error_type: str | None


def _amount_by_freshness(
    provider_states: tuple[TurkeyProviderDebtState, ...], freshness: TurkeyProviderFreshness,
) -> Decimal:
    return sum((state.amount for state in provider_states if state.freshness == freshness), Decimal(0))


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
    car_number). Сумма — НЕ total_amount последнего run'а: см. задачу
    "Turkey debt list: последний успешный результат отдельно по каждому
    провайдеру и госномеру" (production-находка: PARTIAL-run с ошибкой KGM
    считался "достоверным" и его неполный total обнулял/занижал известный
    долг KGM) — total_amount = сумма последних УСПЕШНЫХ результатов GİB и
    KGM по номеру (provider_states, см. TurkeyProviderDebtState), поэтому
    у всех владельцев одного номера она одинакова. is_partial — хотя бы
    один провайдер не CURRENT (STALE/NO_DATA); checked_at — самый свежий
    из использованных успешных результатов."""

    car_id: int
    car_number: str
    owner_telegram_user_id: int
    total_amount: Decimal
    is_partial: bool
    checked_at: datetime
    provider_states: tuple[TurkeyProviderDebtState, ...] = ()

    @property
    def confirmed_amount(self) -> Decimal:
        """Часть total_amount, подтверждённая последней проверкой провайдера (CURRENT)."""
        return _amount_by_freshness(self.provider_states, TurkeyProviderFreshness.CURRENT)

    @property
    def last_known_amount(self) -> Decimal:
        """Часть total_amount, известная только по более ранней успешной проверке (STALE)."""
        return _amount_by_freshness(self.provider_states, TurkeyProviderFreshness.STALE)


@dataclass(frozen=True)
class TurkeyDebtCarGroup:
    """Одна строка "🚨 Задолженность по последней проверке", агрегированная
    по УНИКАЛЬНОМУ car_number (см. задачу "Turkey manager debt statistics
    aggregation") — один физический номер может принадлежать нескольким
    владельцам (turkey_bot_user_cars допускает несколько строк с одним и
    тем же car_number), и раньше debt-список показывал/суммировал его
    отдельно на КАЖДОГО owner. Здесь — ровно одна строка на car_number со
    всеми его владельцами; сумма/состояния провайдеров считаются по номеру
    (см. TurkeyDebtRow), поэтому одинаковы для всех владельцев и НИКОГДА не
    складываются арифметически между ними (см. get_debt_car_groups)."""

    car_number: str
    owner_telegram_user_ids: tuple[int, ...]
    total_amount: Decimal
    is_partial: bool
    checked_at: datetime
    provider_states: tuple[TurkeyProviderDebtState, ...] = ()

    @property
    def confirmed_amount(self) -> Decimal:
        return _amount_by_freshness(self.provider_states, TurkeyProviderFreshness.CURRENT)

    @property
    def last_known_amount(self) -> Decimal:
        return _amount_by_freshness(self.provider_states, TurkeyProviderFreshness.STALE)


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
        📊 Статистика Turkey bot") — НИКАКИХ provider/network requests,
        только SQL reads поверх уже сохранённых turkey_check_runs (см.
        задачу "КРИТИЧЕСКИ ВАЖНО: никаких live check"). cars передаётся
        вызывающим кодом (см. reader/turkey_bot/conversation.py::
        handle_statistics) — этот метод сознательно не тянет
        TurkeyUserCarsRepository как ещё одну constructor-зависимость.

        Сумма машины — см. задачу "Turkey debt list: последний успешный
        результат отдельно по каждому провайдеру и госномеру": для GİB и
        KGM берётся последний УСПЕШНЫЙ результат провайдера по номеру
        (любой владелец/initiator, см. TurkeyCheckRunRepository.
        get_latest_provider_results_for_plate), total_amount — их сумма
        (Avrasya не прибавляется, см. _DEBT_PROVIDERS). Ошибка провайдера
        (в ERROR- или PARTIAL-run) никогда не обнуляет и не уменьшает
        известный долг — он остаётся последним известным (STALE); успешная
        проверка без долга заменяет старый долг этого провайдера. Тот же
        расчёт — у "🔄 Проверить авто с задолженностью" (см.
        TurkeyDebtRefreshService.list_candidates).

        ТОЛЬКО manager/trusted-пути (📊 Статистика, refresh): сумма собрана
        по всем владельцам номера и не должна попадать в self-service
        ответы обычного пользователя.

        Пропускает машину, если по последним успешным результатам долга нет
        (в том числе если успешных результатов не было вовсе — такие номера
        считает count_plates_without_reliable_data). Сортировка — по сумме
        DESC, при равенстве — по свежести проверки DESC."""
        states_by_plate: dict[str, tuple[TurkeyProviderDebtState, ...]] = {}
        rows: list[TurkeyDebtRow] = []
        for car in cars:
            provider_states = states_by_plate.get(car.car_number)
            if provider_states is None:
                provider_states = self._plate_provider_states(car.car_number)
                states_by_plate[car.car_number] = provider_states
            total_amount = sum((state.amount for state in provider_states), Decimal(0))
            if total_amount <= 0:
                continue
            rows.append(
                TurkeyDebtRow(
                    car_id=car.id, car_number=car.car_number, owner_telegram_user_id=car.telegram_user_id,
                    total_amount=total_amount,
                    is_partial=any(state.freshness != TurkeyProviderFreshness.CURRENT for state in provider_states),
                    checked_at=max(
                        state.last_success_at for state in provider_states if state.last_success_at is not None
                    ),
                    provider_states=provider_states,
                )
            )
        rows.sort(key=lambda row: (-row.total_amount, -row.checked_at.timestamp()))
        return rows

    def get_debt_car_groups(self, cars: list[TurkeyUserCar]) -> list[TurkeyDebtCarGroup]:
        """См. TurkeyDebtCarGroup — группирует get_debt_rows() (per-owner
        строки — по-прежнему источник кандидатов refresh, который сохраняет
        результат отдельно на каждого владельца) по car_number. Один
        physical plate = одна строка со всеми его владельцами; сумма и
        состояния провайдеров у строк одного номера одинаковы (считаются по
        номеру), поэтому берутся один раз, а не складываются."""
        rows = self.get_debt_rows(cars)

        groups: dict[str, list[TurkeyDebtRow]] = {}
        for row in rows:
            groups.setdefault(row.car_number, []).append(row)

        result: list[TurkeyDebtCarGroup] = []
        for car_number, owner_rows in groups.items():
            plate_row = owner_rows[0]
            result.append(TurkeyDebtCarGroup(
                car_number=car_number,
                owner_telegram_user_ids=tuple(r.owner_telegram_user_id for r in owner_rows),
                total_amount=plate_row.total_amount, is_partial=plate_row.is_partial,
                checked_at=plate_row.checked_at, provider_states=plate_row.provider_states,
            ))
        result.sort(key=lambda group: (-group.total_amount, -group.checked_at.timestamp()))
        return result

    def count_plates_without_reliable_data(self, cars: list[TurkeyUserCar]) -> int:
        """Сколько УНИКАЛЬНЫХ номеров среди cars НЕ попадает в список
        задолженности и при этом не имеет достоверных данных хотя бы по
        одному из GİB/KGM (NO_DATA: провайдер ни разу не ответил успешно
        или номер вообще не проверялся) — для строки "❔ Нет достоверных
        данных" (см. задачу: отсутствие машины в списке не должно
        читаться как "долга нет", если проверить её так и не удалось).
        Только SQL reads, тот же расчёт, что и get_debt_rows()."""
        count = 0
        for plate in {car.car_number for car in cars}:
            provider_states = self._plate_provider_states(plate)
            if sum((state.amount for state in provider_states), Decimal(0)) > 0:
                continue
            if any(state.freshness == TurkeyProviderFreshness.NO_DATA for state in provider_states):
                count += 1
        return count

    def _plate_provider_states(self, plate: str) -> tuple[TurkeyProviderDebtState, ...]:
        return tuple(self._provider_debt_state(plate, provider) for provider in _DEBT_PROVIDERS)

    def _provider_debt_state(self, plate: str, provider: str) -> TurkeyProviderDebtState:
        last_attempt, last_success = self._runs.get_latest_provider_results_for_plate(plate, provider)
        if last_success is None:
            freshness = TurkeyProviderFreshness.NO_DATA
        elif last_attempt.result.status == ProviderStatus.ERROR:
            freshness = TurkeyProviderFreshness.STALE
        else:
            freshness = TurkeyProviderFreshness.CURRENT
        has_debt = last_success is not None and last_success.result.status == ProviderStatus.HAS_DEBT
        attempt_failed = last_attempt is not None and last_attempt.result.status == ProviderStatus.ERROR
        return TurkeyProviderDebtState(
            provider=provider, freshness=freshness,
            amount=last_success.result.total_amount if has_debt else Decimal(0),
            last_success_at=last_success.finished_at if last_success is not None else None,
            last_attempt_at=last_attempt.finished_at if last_attempt is not None else None,
            last_attempt_error_type=last_attempt.result.error_type if attempt_failed else None,
        )

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
