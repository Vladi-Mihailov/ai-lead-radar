"""TurkeyDebtRefreshService — trusted-manager "🔄 Проверить авто с
задолженностью" (@ProtocolTRbot). НИЧЕГО не знает про Telegram — только
про TurkeyUserCarsRepository/TurkeyCheckRunRepository/
UnifiedTurkeyCheckService/TurkeyStatisticsService.

В ОТЛИЧИЕ от Georgia (@ProtocolGEbot, см. reader/public_bot/
debt_refresh_service.py): police.ge не отслеживает оплату, поэтому там
"задолженность" — это "известные штрафы", монотонно неубывающие. Turkey
unified check (GİB/Avrasya/KGM) — authoritative live-источник: каждый
provider явно отвечает "долг сейчас есть/нет", поэтому УСПЕШНЫЙ ответ
провайдера без долга ДЕЙСТВИТЕЛЬНО означает "долг этого провайдера
погашен", а ошибка провайдера — "неизвестно" (см.
TurkeyStatisticsService.get_debt_rows — последний успешный результат
отдельно по GİB и KGM, никакой своей логики фильтрации здесь нет).

Селекция кандидатов ПОЛНОСТЬЮ переиспользует расчёт debt statistics
block (TurkeyStatisticsService.get_debt_rows — тот же список, что видит
менеджер в 📊 Статистика): машина — кандидат, пока по последним успешным
результатам GİB/KGM у её номера есть долг, в том числе когда последняя
проверка провайдера упала (STALE) — такую машину и нужно перепроверить.
Ничего из этого не переизобретается.

Отсутствие уведомлений владельцам — АРХИТЕКТУРНОЕ свойство: этот класс не
имеет ссылки ни на TurkeyMonitoringService, ни на любой notifier — он
вызывает ИСКЛЮЧИТЕЛЬНО UnifiedTurkeyCheckService.check() +
TurkeyCheckRunRepository.save() + TurkeyUserCarsRepository.
update_last_result(), т.е. РОВНО ту же последовательность, что и
ConversationController._run_manual_check() (см. reader/turkey_bot/
conversation.py) для self-service "🔎 Проверить сейчас" — уведомления
владельцам (🔔 Новая задолженность) отправляет ИСКЛЮЧИТЕЛЬНО
TurkeyMonitoringService._notify_changes(), которую этот класс никогда не
вызывает и на которую физически не может повлиять.

Один provider-check на УНИКАЛЬНЫЙ plate, а не на owner/car-кандидата: один
и тот же турецкий номер объективно означает один физический автомобиль —
GİB/Avrasya/KGM отвечают на вопрос "что должен этот plate", а не "что
должен конкретный наш пользователь". Если один plate сохранён у
НЕСКОЛЬКИХ разных владельцев (см. задачу "DUPLICATE PLATES / OWNERS" —
подтверждённый реальный кейс, см. get_latest_for_owner/
get_latest_provider_results_for_plate), результат ОДНОГО provider-check
переиспользуется и сохраняется ОТДЕЛЬНОЙ turkey_check_runs-строкой для
КАЖДОГО owner (save() поддерживает
это уже сегодня — telegram_user_id/telegram_chat_id передаются отдельно от
самого result, ни одной правки схемы/repository не потребовалось). Это НЕ
искусственная дедупликация: owner-specific storage (одна строка на
(plate, telegram_user_id) пару) сохраняется честно и полностью, экономится
только внешний HTTP-запрос к GİB/Avrasya/KGM, который для одного и того же
plate дал бы идентичный ответ.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from reader.turkey_bot.statistics_service import TurkeyDebtRow, TurkeyStatisticsService
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.models import OverallStatus, ProviderStatus
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

# Тот же лимит попыток на captcha_unavailable/captcha_rejected retry, что и
# у self-service "🔎 Проверить сейчас" (см. ConversationController.
# _MANUAL_MAX_ATTEMPTS) — вся retry/captcha-логика уже реализована в
# UnifiedTurkeyCheckService.check(), здесь переиспользуется тот же режим
# "manual" (никакой новой CAPTCHA/OCR/bypass-логики, см. задачу).
_REFRESH_MAX_ATTEMPTS = 35
_INITIATOR_MANAGER_REFRESH = "manager_refresh"

# См. задачу "reduce Turkey OCR memory pressure" — READ-ONLY диагностика
# production OOM установила: mass refresh может гонять 20+ машин подряд
# БЕЗ паузы, каждая — до 35 CAPTCHA-попыток × 3 провайдера через process-
# wide RapidOCR singleton, что не даёт allocator'у ни единого шанса
# "остыть" между машинами. Пауза здесь — ТОЛЬКО между РАЗНЫМИ машинами
# (не после последней) — provider retry delays (throttle ВНУТРИ одного
# check(), см. AvrasyaSession._throttle) НЕ меняются вовсе.
_INTER_CAR_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class TurkeyRefreshOutcome:
    """Итог одного прогона refresh() (см. задачу "manager Statistics /
    refresh для обоих ботов" п.7) — НИКАКОЙ "погашена"/"осталась"/было-
    стало-аналитики: манагеру нужен только факт "проверено/не удалось
    проверить", а актуальный список — из заново перечитанного persisted
    state (см. ConversationController._build_debt_section). failed
    объединяет ERROR И PARTIAL (см. модуль docstring/задачу "PARTIAL": ни
    то, ни другое не является гарантированно полной проверкой).

    checked/failed/failed_car_numbers — OWNER-ROW семантика (один и тот же
    plate у N owners даёт вклад N в checked), нужна persistence-слою/
    существующим тестам и НЕ предназначена для прямого показа в summary UI
    (см. задачу "Turkey debt-refresh counter/UI fix" — отображение по
    owner-row вместо unique plate было самим багом).

    total_plates/successful_plates/partial_plates/error_plates/
    partial_car_numbers/error_car_numbers — UNIQUE PLATE семантика
    (каждый физический plate ровно один раз, независимо от числа owners),
    посчитана из того же by_plate group, что уже строит refresh() для
    provider-дедупликации — ИМЕННО это summary UI (format_debt_refresh_
    summary) обязан использовать для "Проверено"/"⚠️"/"❌".

    partial_failed_providers — для КАЖДОГО номера из partial_car_numbers
    (тот же порядок) провайдеры, ответившие ошибкой ("gib"/"avrasya"/
    "kgm"), — чтобы summary показывал, КАКОЙ провайдер не ответил (см.
    задачу "Turkey debt list: последний успешный результат отдельно по
    каждому провайдеру и госномеру"). partial_last_success_at — той же
    формы: для каждого такого провайдера момент его последнего УСПЕШНОГО
    ответа по номеру (None — успешных ответов не было никогда), чтобы
    summary показывал и дату (номер мог из списка выпасть совсем)."""

    checked: int
    failed: int
    failed_car_numbers: tuple[str, ...] = ()
    total_plates: int = 0
    successful_plates: int = 0
    partial_plates: int = 0
    error_plates: int = 0
    partial_car_numbers: tuple[str, ...] = ()
    error_car_numbers: tuple[str, ...] = ()
    partial_failed_providers: tuple[tuple[str, ...], ...] = ()
    partial_last_success_at: tuple[tuple[datetime | None, ...], ...] = ()


class TurkeyRefreshAlreadyInProgressError(Exception):
    """См. задачу "CONCURRENCY" — in-memory флаг (см. _in_progress), тот
    же осознанно простой приём, что и у Georgia DebtRefreshService/Turkey
    TurkeyMonitoringJob (один процесс/один event loop, @ProtocolTRbot —
    standalone one-time-checks бот, см. reader/turkey_bot/main.py)."""


class TurkeyDebtRefreshService:
    def __init__(
        self,
        garage_repository: TurkeyUserCarsRepository,
        run_repository: TurkeyCheckRunRepository,
        check_service: UnifiedTurkeyCheckService,
        statistics_service: TurkeyStatisticsService,
        *,
        inter_car_delay_seconds: float = _INTER_CAR_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._garage = garage_repository
        self._runs = run_repository
        self._check_service = check_service
        self._statistics = statistics_service
        self._in_progress = False
        self._inter_car_delay_seconds = inter_car_delay_seconds
        self._sleep = sleep

    def is_in_progress(self) -> bool:
        return self._in_progress

    def list_candidates(self) -> list[TurkeyDebtRow]:
        """ТОЛЬКО чтение — РОВНО тот же расчёт, что и "🚨 Задолженность по
        последней проверке" (см. TurkeyStatisticsService.get_debt_rows —
        последние успешные результаты GİB/KGM по номеру) — используется и
        для превью "Будет проверено автомобилей: N" (см. задачу "FLOW": "до
        нажатия подтверждения НИКАКИХ provider requests"), и внутри
        refresh() для собственно списка проверки."""
        total = self._garage.count_all()
        cars = self._garage.list_all_page(offset=0, limit=total) if total else []
        return self._statistics.get_debt_rows(cars)

    async def refresh(self) -> TurkeyRefreshOutcome:
        """См. задачу "СЕМАНТИКА"/"PARTIAL"/"DUPLICATE PLATES": группирует
        кандидатов по car_number (один physical plate = один provider
        check, см. модуль docstring), сохраняет результат ОТДЕЛЬНО для
        КАЖДОГО owner этого plate (см. TurkeyCheckRunRepository.save() —
        уже поддерживает несколько сохранений одного result под разными
        telegram_user_id). ERROR/PARTIAL -> failed (не гарантированно
        полная проверка, см. задачу "PARTIAL" — summary честно показывает,
        что проверка была неполной, и какие провайдеры не ответили). Ни
        ERROR, ни PARTIAL не стирают известный долг: список считается по
        последнему успешному результату КАЖДОГО провайдера (см.
        TurkeyStatisticsService.get_debt_rows), поэтому упавший провайдер
        остаётся с последней известной суммой, а успешно ответившие
        провайдеры того же PARTIAL-run обновляют свою. Актуальный список
        после refresh — заново перечитанный persisted state (см.
        list_candidates()/ConversationController._build_debt_section), не
        накапливается здесь построчно."""
        if self._in_progress:
            raise TurkeyRefreshAlreadyInProgressError()

        self._in_progress = True
        try:
            candidates = self.list_candidates()

            by_plate: dict[str, list[TurkeyDebtRow]] = {}
            for row in candidates:
                by_plate.setdefault(row.car_number, []).append(row)

            failed = 0
            failed_car_numbers: list[str] = []
            successful_plates = 0
            partial_plates = 0
            error_plates = 0
            partial_car_numbers: list[str] = []
            partial_failed_providers: list[tuple[str, ...]] = []
            partial_last_success_at: list[tuple[datetime | None, ...]] = []
            error_car_numbers: list[str] = []

            for index, (plate, rows) in enumerate(by_plate.items()):
                if index > 0:
                    # См. модульный докстрок про _INTER_CAR_DELAY_SECONDS —
                    # пауза МЕЖДУ РАЗНЫМИ машинами, не после последней.
                    await self._sleep(self._inter_car_delay_seconds)

                result = await self._check_service.check(
                    plate, max_attempts=_REFRESH_MAX_ATTEMPTS, mode="manual",
                )
                is_failed = result.overall_status in (OverallStatus.ERROR, OverallStatus.PARTIAL)
                if is_failed:
                    failed += len(rows)
                    failed_car_numbers.append(plate)

                # Unique-plate классификация (см. TurkeyRefreshOutcome
                # docstring) — ОДИН инкремент на plate, НЕ на owner-row,
                # ERROR и PARTIAL остаются различимы (см. задачу "Keep
                # these distinct in UI" — PARTIAL != полный провал).
                if result.overall_status == OverallStatus.ERROR:
                    error_plates += 1
                    error_car_numbers.append(plate)
                elif result.overall_status == OverallStatus.PARTIAL:
                    partial_plates += 1
                    partial_car_numbers.append(plate)
                    partial_failed_providers.append(tuple(
                        p.provider for p in result.providers if p.status == ProviderStatus.ERROR
                    ))
                else:
                    successful_plates += 1

                for row in rows:
                    self._runs.save(
                        result, telegram_user_id=row.owner_telegram_user_id,
                        telegram_chat_id=row.owner_telegram_user_id,
                        initiator=_INITIATOR_MANAGER_REFRESH,
                    )
                    self._garage.update_last_result(
                        telegram_user_id=row.owner_telegram_user_id, car_number=plate,
                        overall_status=result.overall_status.value, total_amount=result.total_amount,
                    )

                if result.overall_status == OverallStatus.PARTIAL:
                    # Только чтение уже сохранённой истории — дата последнего
                    # УСПЕШНОГО ответа каждого упавшего провайдера (см.
                    # TurkeyRefreshOutcome.partial_last_success_at).
                    partial_last_success_at.append(tuple(
                        self._last_success_at(plate, provider) for provider in partial_failed_providers[-1]
                    ))

            return TurkeyRefreshOutcome(
                checked=len(candidates), failed=failed, failed_car_numbers=tuple(failed_car_numbers),
                total_plates=len(by_plate), successful_plates=successful_plates,
                partial_plates=partial_plates, error_plates=error_plates,
                partial_car_numbers=tuple(partial_car_numbers), error_car_numbers=tuple(error_car_numbers),
                partial_failed_providers=tuple(partial_failed_providers),
                partial_last_success_at=tuple(partial_last_success_at),
            )
        finally:
            self._in_progress = False

    def _last_success_at(self, plate: str, provider: str) -> datetime | None:
        _last_attempt, last_success = self._runs.get_latest_provider_results_for_plate(plate, provider)
        return last_success.finished_at if last_success is not None else None
