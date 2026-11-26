"""Операции MAX-бота Турции поверх ОБЩИХ компонентов Telegram-бота:
UnifiedTurkeyCheckService (GİB, Avrasya, KGM — те же провайдеры, ретраи,
маршрут KGM через relay) и TurkeyCheckRunRepository (общая история проверок
номера). Отдельно — только MAX-машины, мониторинг и снимки
(reader/max_bot/turkey/repository.py).

Проверки из MAX сохраняются в turkey_check_runs с telegram_user_id =
telegram_chat_id = MAX_RUN_OWNER (0): базовая точка Telegram-подписчиков
выбирается по их telegram_user_id и такие проверки не видит, а менеджерский
список задолженности (по номеру, по всем проверкам) получает свежие данные.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from reader.max_bot.turkey.repository import MaxTrCar, MaxTrMonitoring, MaxTurkeyRepository
from reader.turkey_bot.monitoring.scheduler_job import TURKEY_MONITORING_TZ
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.models import ProviderStatus, UnifiedCheckResult
from reader.turkey_bot.unified.run_repository import ProviderResultAt, TurkeyCheckRunRepository

MAX_RUN_OWNER = 0
INITIATOR_MANUAL = "max_manual"
PROVIDERS = ("gib", "avrasya", "kgm")
TELEGRAM_MONITORING_SLOT = (21, 0)
# Тот же лимит попыток на провайдера, что у ручной проверки Telegram-бота
# (reader/turkey_bot/conversation.py::_MANUAL_MAX_ATTEMPTS).
_MANUAL_MAX_ATTEMPTS = 35
_HISTORY_LIMIT = 10


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def last_telegram_slot_start(now: datetime) -> datetime:
    """Последний наступивший слот 21:00 Europe/Istanbul (UTC)."""
    local = now.astimezone(TURKEY_MONITORING_TZ)
    hour, minute = TELEGRAM_MONITORING_SLOT
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=1)
    return slot.astimezone(timezone.utc)


class _InProgress:
    def __repr__(self) -> str:
        return "CHECK_IN_PROGRESS"


CHECK_IN_PROGRESS = _InProgress()


@dataclass(frozen=True)
class PlateOverview:
    """Экран менеджера «🔎 Поиск авто» — только чтение БД."""

    plate: str
    max_cars: int
    max_monitoring: int
    latest: dict[str, ProviderResultAt | None]


class MaxTurkeyService:
    def __init__(
        self,
        repository: MaxTurkeyRepository,
        run_repository: TurkeyCheckRunRepository,
        check_service: UnifiedTurkeyCheckService,
    ):
        self.repository = repository
        self._runs = run_repository
        self._check_service = check_service
        self._checks_in_progress: set[str] = set()

    # ---- «Мои авто» ----

    def add_car(self, *, max_user_id: int, plate: str) -> tuple[MaxTrCar, bool]:
        return self.repository.add_car(max_user_id=max_user_id, plate=plate)

    def list_cars(self, max_user_id: int) -> list[MaxTrCar]:
        return self.repository.list_cars(max_user_id)

    def get_owned_car(self, car_id: int, *, max_user_id: int) -> MaxTrCar | None:
        return self.repository.get_owned_car(car_id, max_user_id=max_user_id)

    def monitoring_active(self, max_user_id: int, plate: str) -> bool:
        monitoring = self.repository.get_monitoring(max_user_id, plate)
        return monitoring is not None and monitoring.active

    def delete_car(self, car: MaxTrCar) -> bool:
        return self.repository.delete_car(car.id, max_user_id=car.max_user_id)

    # ---- мониторинг ----

    def enable_monitoring(self, car: MaxTrCar, *, now: datetime | None = None) -> MaxTrMonitoring:
        """Базовая точка — СВЕЖИЙ успешный результат провайдера по номеру
        (получен после последнего слота 21:00: ручная проверка владельца или
        плановый прогон). Старый результат базой не служит: задолженность,
        возникшая между ним и включением, пришла бы как «новая», хотя она
        старше подписки. Провайдер без свежего результата получит базовую
        точку при первой успешной проверке — без уведомления (monitoring.py)."""
        monitoring = self.repository.enable(max_user_id=car.max_user_id, plate=car.plate)
        self.repository.clear_seen(monitoring.id)
        fresh_since = last_telegram_slot_start(now or datetime.now(timezone.utc))
        for provider in PROVIDERS:
            _last_attempt, last_success = self._runs.get_latest_provider_results_for_plate(car.plate, provider)
            if last_success is not None and _as_utc(last_success.finished_at) >= fresh_since:
                self.repository.set_seen(monitoring.id, last_success.result)
        return monitoring

    def disable_monitoring(self, car: MaxTrCar) -> None:
        self.repository.disable(max_user_id=car.max_user_id, plate=car.plate)

    # ---- проверка ----

    async def check_now(self, car: MaxTrCar):
        """UnifiedCheckResult | CHECK_IN_PROGRESS (этот номер уже проверяется)."""
        if car.plate in self._checks_in_progress:
            return CHECK_IN_PROGRESS
        self._checks_in_progress.add(car.plate)
        try:
            result = await self._check_service.check(car.plate, max_attempts=_MANUAL_MAX_ATTEMPTS, mode="manual")
        finally:
            self._checks_in_progress.discard(car.plate)
        self._runs.save(
            result, telegram_user_id=MAX_RUN_OWNER, telegram_chat_id=MAX_RUN_OWNER, initiator=INITIATOR_MANUAL,
        )
        self.repository.update_last_result(
            max_user_id=car.max_user_id, plate=car.plate,
            overall_status=result.overall_status.value, total_amount=result.total_amount,
        )
        self._mark_seen_by_owner(car, result)
        return result

    def _mark_seen_by_owner(self, car: MaxTrCar, result: UnifiedCheckResult) -> None:
        """Ручную проверку владелец видел на экране — плановый прогон не
        повторяет её уведомлением (та же семантика, что у Telegram-бота)."""
        monitoring = self.repository.get_monitoring(car.max_user_id, car.plate)
        if monitoring is None or not monitoring.active:
            return
        for provider_result in result.providers:
            if provider_result.status != ProviderStatus.ERROR:
                self.repository.set_seen(monitoring.id, provider_result)

    def history(self, plate: str) -> list[UnifiedCheckResult]:
        return self._runs.list_by_plate(plate, limit=_HISTORY_LIMIT)

    # ---- перенос из Telegram ----

    def import_cars(self, max_user_id: int, cars: list[tuple[str, bool]]) -> tuple[list[str], int]:
        """(перенесённые номера, уже были в списке). Мониторинг включается
        для машин, у которых он был включён в Telegram, — с базовой точкой
        (без исторических уведомлений). Telegram-данные не меняются."""
        imported, skipped = [], 0
        for plate, monitoring_was_active in cars:
            car, created = self.repository.add_car(max_user_id=max_user_id, plate=plate)
            if not created:
                skipped += 1
                continue
            imported.append(plate)
            if monitoring_was_active:
                self.enable_monitoring(car)
        return imported, skipped

    # ---- менеджеры ----

    def plate_overview(self, plate: str) -> PlateOverview:
        latest = {
            provider: self._runs.get_latest_provider_results_for_plate(plate, provider)[1] for provider in PROVIDERS
        }
        return PlateOverview(
            plate=plate, max_cars=self.repository.count_cars_for_plate(plate),
            max_monitoring=len(self.repository.active_for_plate(plate)), latest=latest,
        )
