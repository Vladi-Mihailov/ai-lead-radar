"""TurkeyDebtRefreshService — trusted-manager "🔄 Проверить авто с
задолженностью" (@ProtocolTRbot). НИЧЕГО не знает про Telegram — только
про TurkeyUserCarsRepository/TurkeyCheckRunRepository/
UnifiedTurkeyCheckService/TurkeyStatisticsService.

В ОТЛИЧИЕ от Georgia (@ProtocolGEbot, см. reader/public_bot/
debt_refresh_service.py): police.ge не отслеживает оплату, поэтому там
"задолженность" — это "известные штрафы", монотонно неубывающие. Turkey
unified check (GİB/Avrasya/KGM) — authoritative live-источник: каждый
provider явно отвечает "долг сейчас есть/нет", поэтому здесь
total_amount=0 у НОВОГО достоверного результата ДЕЙСТВИТЕЛЬНО означает
"задолженность погашена" — машина корректно исчезает из debt-списка (см.
TurkeyStatisticsService.get_debt_rows — total_amount <= 0 уже
исключается, никакой новой логики фильтрации здесь не нужно).

Селекция кандидатов ПОЛНОСТЬЮ переиспользует authoritative selection
semantics debt statistics block (TurkeyStatisticsService.get_debt_rows +
TurkeyCheckRunRepository.get_latest_reliable_for_owner) — ERROR никогда не
"последнее состояние", total_amount<=0 не кандидат, задачи без ни одной
достоверной проверки не кандидаты. Ничего из этого не переизобретается.

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
подтверждённый реальный кейс, см. get_latest_for_owner/get_latest_reliable_
for_owner), результат ОДНОГО provider-check переиспользуется и сохраняется
ОТДЕЛЬНОЙ turkey_check_runs-строкой для КАЖДОГО owner (save() поддерживает
это уже сегодня — telegram_user_id/telegram_chat_id передаются отдельно от
самого result, ни одной правки схемы/repository не потребовалось). Это НЕ
искусственная дедупликация: owner-specific storage (одна строка на
(plate, telegram_user_id) пару) сохраняется честно и полностью, экономится
только внешний HTTP-запрос к GİB/Avrasya/KGM, который для одного и того же
plate дал бы идентичный ответ.
"""

from dataclasses import dataclass
from decimal import Decimal

from reader.turkey_bot.statistics_service import TurkeyDebtRow, TurkeyStatisticsService
from reader.turkey_bot.unified.check_service import UnifiedTurkeyCheckService
from reader.turkey_bot.unified.models import OverallStatus
from reader.turkey_bot.unified.run_repository import TurkeyCheckRunRepository
from reader.turkey_bot.user_cars_repository import TurkeyUserCarsRepository

# Тот же лимит попыток на captcha_unavailable/captcha_rejected retry, что и
# у self-service "🔎 Проверить сейчас" (см. ConversationController.
# _MANUAL_MAX_ATTEMPTS) — вся retry/captcha-логика уже реализована в
# UnifiedTurkeyCheckService.check(), здесь переиспользуется тот же режим
# "manual" (никакой новой CAPTCHA/OCR/bypass-логики, см. задачу).
_REFRESH_MAX_ATTEMPTS = 35
_INITIATOR_MANAGER_REFRESH = "manager_refresh"


@dataclass(frozen=True)
class TurkeyRefreshOutcome:
    """Итог одного прогона refresh() — см. задачу "RESULT". incomplete
    объединяет ERROR И PARTIAL (см. модуль docstring/задачу "PARTIAL": ни
    то, ни другое не является гарантированно полной проверкой) —
    total_before/total_after — суммы по debt-списку ДО и ПОСЛЕ (см.
    задачу: "если сумма изменилась, можно дополнительно показать было/
    стало"), пересчитаны честно из БД, а не накоплены построчно во время
    цикла (см. refresh())."""

    checked: int
    paid: int
    remains: int
    incomplete: int
    total_before: Decimal
    total_after: Decimal


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
    ):
        self._garage = garage_repository
        self._runs = run_repository
        self._check_service = check_service
        self._statistics = statistics_service
        self._in_progress = False

    def is_in_progress(self) -> bool:
        return self._in_progress

    def list_candidates(self) -> list[TurkeyDebtRow]:
        """ТОЛЬКО чтение — та же authoritative selection semantics, что и
        "🚨 Задолженность по последней проверке" (см.
        TurkeyStatisticsService.get_debt_rows) — используется и для
        превью "Будет проверено автомобилей: N" (см. задачу "FLOW": "до
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
        telegram_user_id), классифицирует по authoritative overall_status/
        total_amount НОВОГО результата:
          ERROR/PARTIAL -> incomplete (не гарантированно полная проверка,
            предыдущее достоверное состояние НЕ считается погашенным —
            get_latest_reliable_for_owner сама пропустит ERROR-строку и
            вернёт прежний reliable результат; PARTIAL, наоборот, СТАНОВИТСЯ
            новым reliable состоянием, как и везде в unified-архитектуре,
            но помечается incomplete именно в ЭТОМ summary, см. задачу:
            "summary должен честно показать, что проверка была неполной");
          total_amount <= 0 (полный SUCCESS) -> paid;
          total_amount > 0 (полный SUCCESS) -> remains."""
        if self._in_progress:
            raise TurkeyRefreshAlreadyInProgressError()

        self._in_progress = True
        try:
            candidates = self.list_candidates()
            total_before = sum((row.total_amount for row in candidates), Decimal(0))

            by_plate: dict[str, list[TurkeyDebtRow]] = {}
            for row in candidates:
                by_plate.setdefault(row.car_number, []).append(row)

            paid = 0
            remains = 0
            incomplete = 0

            for plate, rows in by_plate.items():
                result = await self._check_service.check(
                    plate, max_attempts=_REFRESH_MAX_ATTEMPTS, mode="manual",
                )

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

                    if result.overall_status in (OverallStatus.ERROR, OverallStatus.PARTIAL):
                        incomplete += 1
                    elif result.total_amount <= 0:
                        paid += 1
                    else:
                        remains += 1

            total_after = sum((row.total_amount for row in self.list_candidates()), Decimal(0))

            return TurkeyRefreshOutcome(
                checked=len(candidates), paid=paid, remains=remains, incomplete=incomplete,
                total_before=total_before, total_after=total_after,
            )
        finally:
            self._in_progress = False
