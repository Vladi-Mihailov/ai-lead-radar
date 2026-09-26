"""DebtRefreshService — trusted-manager "🔄 Проверить авто со штрафами"
(@ProtocolGEbot). НИЧЕГО не знает про Telegram — только про
FineMonitoringTaskRepository/FineCheckService.

"🚨 Штрафы по последней проверке" (см. задачу "manager Statistics /
refresh для обоих ботов") = "что показала последняя УСПЕШНАЯ проверка"
(FineMonitoringTask.last_successful_total_amount/last_successful_checked_at,
см. FineMonitoringTaskRepository.list_tasks_with_known_debt()) — НЕ сумма
истории detected_fines (та остаётся отдельной, append-only history/dedup
таблицей, никогда не источником отображаемого состояния). В отличие от
предыдущей, монотонно-неубывающей "известные штрафы"-модели:
  - новая успешная проверка ПОЛНОСТЬЮ ЗАМЕНЯЕТ предыдущее значение
    (может уменьшиться, увеличиться или стать 0);
  - total_amount == 0 у НОВОЙ успешной проверки означает "задача исчезает
    из debt-списка" (list_tasks_with_known_debt() сам её не вернёт);
  - ERROR никогда не трогает last_successful_total_amount — это гарантирует
    ИСКЛЮЧИТЕЛЬНО FineCheckService.check_task() (единственный писатель),
    без отдельной ветки отката здесь.

Persistence этого поля происходит ВНУТРИ FineCheckService.check_task() —
ЕДИНСТВЕННОГО authoritative layer'а, вызываемого ЛЮБЫМ путём проверки
(мониторинг/manual "Проверить сейчас"/Add Car/manager refresh, см. задачу
п.11) — поэтому refresh() здесь НЕ вызывает record_successful_check()
самостоятельно (в отличие от более ранней версии этого файла): он просто
ПОЛУЧАЕТ уже персистентный результат, вызвав тот же check_task().

Отсутствие уведомлений пользователю — АРХИТЕКТУРНОЕ свойство, а не
дисциплина вызывающего кода: этот класс не имеет ссылки ни на
NotificationService, ни на FineNotificationCoordinator, поэтому физически
не может ничего разослать. Реально новый штраф, найденный во время
refresh, останется в detected_fines с notification_sent_at IS NULL и будет
доставлен ОБЫЧНЫМ следующим проходом FineJob.flush_pending() — то же самое
поведение, что и у уже существующего SubscriptionService.check_now()/
check_now_task() (см. design report), не новый случай.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from reader.fines.check_service import FineCheckService
from reader.fines.models import FineTaskDebtSnapshot
from reader.fines.task_repository import FineMonitoringTaskRepository

# См. задачу "guard Georgia debt against false zero results" п.5 — та же
# пауза, что и false-zero confirmation delay в FineCheckService (см.
# reader/fines/check_service.py), но здесь она РАЗДЕЛЯЕТ РАЗНЫЕ машины
# (не connection ко второй попытке для ОДНОЙ машины) — mass refresh
# перестаёт бить police.ge back-to-back без пауз, что и создавало условия
# для false-empty ответов под нагрузкой (см. production incident).
_INTER_CAR_DELAY_SECONDS = 5.0


@dataclass(frozen=True)
class DebtSummary:
    car_count: int
    total_amount: float


@dataclass(frozen=True)
class RefreshOutcome:
    """Итог одного прогона refresh() (см. задачу п.7 "RESULT") — НИКАКОЙ
    "increased"/"unchanged"/"paid"/"remains"-аналитики: манагеру нужен
    только факт "проверено/не удалось проверить", а актуальный список —
    из заново перечитанного persisted state (см.
    ConversationController._build_debt_section)."""

    checked: int
    failed: int
    failed_car_numbers: tuple[str, ...] = field(default_factory=tuple)


class RefreshAlreadyInProgressError(Exception):
    """См. задачу п.6 "CONCURRENCY" — in-memory флаг (см. _in_progress),
    ТОЛЬКО для concurrency lock, НЕ как источник debt/fines state (это
    всегда БД, см. модульный докстрок) — тот же осознанно простой приём,
    что и FineJob._last_run_slot: у @ProtocolGEbot один процесс/один event
    loop, второй запуск поверх уже выполняющегося физически может прийти
    только вторым нажатием той же кнопки в том же процессе."""


class DebtRefreshService:
    def __init__(
        self,
        task_repository: FineMonitoringTaskRepository,
        check_service: FineCheckService,
        *,
        inter_car_delay_seconds: float = _INTER_CAR_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._task_repository = task_repository
        self._check_service = check_service
        self._in_progress = False
        self._inter_car_delay_seconds = inter_car_delay_seconds
        self._sleep = sleep

    def is_in_progress(self) -> bool:
        return self._in_progress

    def list_debt_rows(self) -> list[FineTaskDebtSnapshot]:
        """Только чтение, ВСЕГДА заново из БД (см. задачу п.5/п.8/п.9:
        "refresh должен быть stateful" / "next-refresh candidates из
        ЭТОГО сохранённого состояния" / "restart не теряет state") —
        используется и для "🚨 Штрафы по последней проверке" в 📊
        Статистика, и для превью "Будет проверено автомобилей: N" перед
        refresh, и как ИСТОЧНИК candidates внутри refresh() самого себя —
        ни одного provider/network запроса, ни одного in-memory кэша."""
        return self._task_repository.list_tasks_with_known_debt()

    def get_debt_summary(self) -> DebtSummary:
        rows = self.list_debt_rows()
        return DebtSummary(
            car_count=len(rows), total_amount=sum(row.total_amount for row in rows),
        )

    async def refresh(self) -> RefreshOutcome:
        """Кандидаты — ТОЛЬКО tasks с известной (persisted) суммой > 0
        (см. list_debt_rows()/list_tasks_with_known_debt()), по одному
        police.ge-запросу на task_id — тот же authoritative
        FineCheckService.check_task(), без вызова flush_pending() (см.
        модульный докстрок про отсутствие уведомлений). check_task() САМ
        персистит новое last_successful_total_amount при status=='ok' и
        НИКОГДА не трогает его при ERROR — "сохранить старое достоверное
        состояние" получается ПО ПОСТРОЕНИЮ, без отдельной ветки отката
        здесь."""
        if self._in_progress:
            raise RefreshAlreadyInProgressError()

        self._in_progress = True
        try:
            task_ids = [row.task_id for row in self.list_debt_rows()]

            failed = 0
            failed_car_numbers: list[str] = []

            for index, task_id in enumerate(task_ids):
                if index > 0:
                    # См. задачу п.5 — пауза МЕЖДУ РАЗНЫМИ машинами (не
                    # после последней, см. модульный докстрок про
                    # _INTER_CAR_DELAY_SECONDS) — mass refresh больше не
                    # бьёт police.ge back-to-back без пауз.
                    await self._sleep(self._inter_car_delay_seconds)

                task = self._task_repository.get(task_id)
                if task is None:
                    # Задача удалена конкурентно между чтением списка и
                    # проверкой — просто пропускаем, не считаем ни успехом,
                    # ни ошибкой (её больше нет вообще).
                    continue

                result = await self._check_service.check_task(task)
                if result.status == "error":
                    failed += 1
                    failed_car_numbers.append(task.car_number)
                # status == "ok": check_task() уже сам записал новое
                # last_successful_total_amount (включая false-zero guard,
                # см. reader/fines/check_service.py) — здесь ничего
                # дополнительно персистить не нужно (см. модульный докстрок).

            return RefreshOutcome(
                checked=len(task_ids), failed=failed, failed_car_numbers=tuple(failed_car_numbers),
            )
        finally:
            self._in_progress = False
